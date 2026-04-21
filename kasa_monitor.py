#!/usr/bin/env python3
"""TP-Link HS110 / Kasa smart plug discovery, polling and persistence.

The HomeWizard P1 meter already measures whole-house power at the main fuse.
HS110 plugs give per-appliance breakdowns for anything we care to tag (sauna
fan, heat pumps, EV charger, etc). This module:

  - discovers plugs on configured /24 subnets via unicast probe
  - polls known plugs concurrently with asyncio
  - stores readings into the same p1_data.db used by the web monitor

python-kasa 0.10 deprecated `emeter_realtime` in favour of the Energy module,
but the old accessor still works and is ~3x less code; revisit when removed.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from kasa import Discover

DEFAULT_SUBNETS = ["192.168.11.0/24"]
SCAN_CONCURRENCY = 50
SCAN_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 4


def ensure_kasa_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kasa_plugs (
                mac TEXT PRIMARY KEY,
                ip TEXT NOT NULL,
                alias TEXT,
                model TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                discovered_at REAL,
                last_seen_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kasa_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                datetime TEXT NOT NULL,
                mac TEXT NOT NULL,
                alias TEXT,
                power_w REAL,
                voltage_v REAL,
                current_a REAL,
                total_kwh REAL,
                is_on INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kasa_readings_mac_ts ON kasa_readings (mac, timestamp)"
        )
        conn.commit()
    finally:
        conn.close()


def _iter_ips(cidr: str) -> List[str]:
    """Expand a /24 (or bare 'a.b.c.0') CIDR into 254 host IPs."""
    if "/" in cidr:
        network, prefix = cidr.split("/")
        if prefix != "24":
            raise ValueError(f"Only /24 subnets supported, got: {cidr}")
    else:
        network = cidr
    base = network.rsplit(".", 1)[0]
    return [f"{base}.{i}" for i in range(1, 255)]


async def _probe(ip: str) -> Optional[Dict[str, Any]]:
    try:
        dev = await asyncio.wait_for(
            Discover.discover_single(ip), timeout=SCAN_TIMEOUT_SECONDS
        )
        if not dev:
            return None
        await dev.update()
        if not getattr(dev, "has_emeter", False):
            return None
        return {
            "ip": ip,
            "mac": dev.mac,
            "alias": (dev.alias or "").strip(),
            "model": dev.model,
        }
    except Exception:
        return None


async def _scan(subnets: List[str]) -> List[Dict[str, Any]]:
    ips: List[str] = []
    for cidr in subnets:
        ips.extend(_iter_ips(cidr))
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def bounded(ip: str):
        async with sem:
            return await _probe(ip)

    results = await asyncio.gather(*[bounded(ip) for ip in ips])
    return [r for r in results if r]


def discover_plugs(subnets: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Synchronous wrapper: scan subnets and return plug descriptors."""
    return asyncio.run(_scan(subnets or DEFAULT_SUBNETS))


def upsert_plugs(db_path: str, plugs: List[Dict[str, Any]]) -> None:
    if not plugs:
        return
    ensure_kasa_tables(db_path)
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        for p in plugs:
            conn.execute(
                """
                INSERT INTO kasa_plugs (mac, ip, alias, model, enabled, discovered_at, last_seen_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    ip=excluded.ip,
                    alias=CASE
                        WHEN excluded.alias IS NOT NULL AND excluded.alias != ''
                        THEN excluded.alias
                        ELSE kasa_plugs.alias
                    END,
                    model=excluded.model,
                    last_seen_at=excluded.last_seen_at
                """,
                (p["mac"], p["ip"], p["alias"], p["model"], now, now),
            )
        conn.commit()
    finally:
        conn.close()


def list_plugs(db_path: str, only_enabled: bool = False) -> List[Dict[str, Any]]:
    ensure_kasa_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT mac, ip, alias, model, enabled, discovered_at, last_seen_at "
            "FROM kasa_plugs"
        )
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY COALESCE(NULLIF(alias, ''), ip)"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [
        {
            "mac": r[0],
            "ip": r[1],
            "alias": r[2],
            "model": r[3],
            "enabled": bool(r[4]),
            "discovered_at": r[5],
            "last_seen_at": r[6],
        }
        for r in rows
    ]


async def _read_plug(ip: str, mac_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        dev = await asyncio.wait_for(
            Discover.discover_single(ip), timeout=READ_TIMEOUT_SECONDS
        )
        if not dev:
            return None
        await dev.update()
        if not getattr(dev, "has_emeter", False):
            return None
        e = dev.emeter_realtime
        return {
            "ip": ip,
            "mac": dev.mac or mac_hint,
            "alias": (dev.alias or "").strip(),
            "power_w": float(e.power or 0.0),
            "voltage_v": float(e.voltage or 0.0),
            "current_a": float(e.current or 0.0),
            "total_kwh": float(e.total or 0.0),
            "is_on": bool(dev.is_on),
        }
    except Exception:
        return None


async def _read_all(targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = await asyncio.gather(
        *[_read_plug(t["ip"], t.get("mac")) for t in targets]
    )
    return [r for r in results if r]


def poll_plugs(db_path: str) -> List[Dict[str, Any]]:
    """Read all enabled plugs, persist readings, update last_seen_at, return list."""
    plugs = list_plugs(db_path, only_enabled=True)
    if not plugs:
        return []
    readings = asyncio.run(_read_all(plugs))
    if not readings:
        return []

    now_ts = time.time()
    now_iso = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for r in readings:
            conn.execute(
                """
                INSERT INTO kasa_readings
                (timestamp, datetime, mac, alias, power_w, voltage_v, current_a, total_kwh, is_on)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts,
                    now_iso,
                    r["mac"],
                    r["alias"],
                    r["power_w"],
                    r["voltage_v"],
                    r["current_a"],
                    r["total_kwh"],
                    1 if r["is_on"] else 0,
                ),
            )
            conn.execute(
                "UPDATE kasa_plugs SET last_seen_at = ? WHERE mac = ?",
                (now_ts, r["mac"]),
            )
        conn.commit()
    finally:
        conn.close()

    for r in readings:
        r["timestamp"] = now_ts
        r["datetime"] = now_iso
    return readings


def get_latest_readings(db_path: str) -> List[Dict[str, Any]]:
    """Most recent reading per plug (joined with plug metadata)."""
    ensure_kasa_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.mac, p.ip, p.alias, p.model, p.enabled, p.last_seen_at,
                   r.timestamp, r.datetime, r.power_w, r.voltage_v,
                   r.current_a, r.total_kwh, r.is_on
            FROM kasa_plugs p
            LEFT JOIN kasa_readings r ON r.id = (
                SELECT id FROM kasa_readings WHERE mac = p.mac
                ORDER BY timestamp DESC LIMIT 1
            )
            ORDER BY COALESCE(NULLIF(p.alias, ''), p.ip)
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "mac": r[0],
            "ip": r[1],
            "alias": r[2],
            "model": r[3],
            "enabled": bool(r[4]),
            "last_seen_at": r[5],
            "timestamp": r[6],
            "datetime": r[7],
            "power_w": r[8],
            "voltage_v": r[9],
            "current_a": r[10],
            "total_kwh": r[11],
            "is_on": bool(r[12]) if r[12] is not None else None,
        }
        for r in rows
    ]


def get_history(
    db_path: str,
    start_ts: float,
    end_ts: float,
    mac: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ensure_kasa_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        if mac:
            rows = conn.execute(
                """
                SELECT timestamp, datetime, mac, alias, power_w, voltage_v,
                       current_a, total_kwh, is_on
                FROM kasa_readings
                WHERE mac = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
                LIMIT 20000
                """,
                (mac, start_ts, end_ts),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT timestamp, datetime, mac, alias, power_w, voltage_v,
                       current_a, total_kwh, is_on
                FROM kasa_readings
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
                LIMIT 50000
                """,
                (start_ts, end_ts),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "timestamp": r[0],
            "datetime": r[1],
            "mac": r[2],
            "alias": r[3],
            "power_w": r[4],
            "voltage_v": r[5],
            "current_a": r[6],
            "total_kwh": r[7],
            "is_on": bool(r[8]) if r[8] is not None else None,
        }
        for r in rows
    ]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        found = discover_plugs()
        print(f"Found {len(found)} plug(s):")
        for p in found:
            print(f"  {p['ip']:15}  {p['mac']}  {p['model']}  {p['alias']}")
        upsert_plugs("p1_data.db", found)
    elif len(sys.argv) > 1 and sys.argv[1] == "poll":
        readings = poll_plugs("p1_data.db")
        for r in readings:
            print(
                f"{r['alias'] or r['mac']:25}  {r['power_w']:6.1f}W  "
                f"{r['voltage_v']:5.1f}V  {r['current_a']:5.2f}A  "
                f"total={r['total_kwh']:.3f}kWh  on={r['is_on']}"
            )
    else:
        print("Usage: kasa_monitor.py {discover|poll}")
