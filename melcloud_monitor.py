#!/usr/bin/env python3
"""Mitsubishi MELCloud polling → SQLite.

Uses `pymelcloud` (reverse-engineered classic API) to pull state from the
Mitsubishi U6/U6 Lite WiFi adapters via cloud. No local API exists on these
adapters; see research notes in CLAUDE.md.

Reads credentials from env vars `MELCLOUD_USER` / `MELCLOUD_PASS`. The
`ContextKey` (session token) is cached in module state and re-acquired on
auth failure. Poll cadence defaults to 120s — MELCloud rate-limits aggressive
polling.

Data fields captured per unit (ATA heat pumps):
  power, operation_mode, room_temperature, target_temperature,
  outdoor_temperature, fan_speed, vane_horizontal, vane_vertical,
  total_energy_consumed_kwh (lifetime), wifi_signal_dbm, last_communication
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import pymelcloud

DB_PATH = "p1_data.db"
DEFAULT_POLL_INTERVAL_SECONDS = 120
LOGIN_RETRY_BACKOFF_SECONDS = 60
ENERGY_REPORT_URL = "https://app.melcloud.com/Mitsubishi.Wifi.Client/EnergyCost/Report"
# MELCloud returns hourly granularity (LabelType=0) when requested range is
# ≤ ~2 days; daily granularity (LabelType=1) for longer ranges.
ENERGY_HOURLY_SEGMENT_HOURS = 48

_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_token_acquired_at: float = 0.0


def ensure_melcloud_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS melcloud_devices (
                device_id INTEGER PRIMARY KEY,
                building_id INTEGER,
                serial TEXT,
                mac TEXT,
                kind TEXT,          -- ata | atw | erv
                name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                discovered_at REAL,
                last_seen_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS melcloud_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                datetime TEXT NOT NULL,
                device_id INTEGER NOT NULL,
                name TEXT,
                power INTEGER,
                operation_mode TEXT,
                room_temperature REAL,
                target_temperature REAL,
                outdoor_temperature REAL,
                fan_speed TEXT,
                vane_horizontal TEXT,
                vane_vertical TEXT,
                total_energy_consumed_kwh REAL,
                wifi_signal_dbm INTEGER,
                last_communication TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_melcloud_readings_dev_ts "
            "ON melcloud_readings (device_id, timestamp)"
        )
        # Per-hour energy breakdown from MELCloud Reports API (by operation mode)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS melcloud_energy_hourly (
                device_id INTEGER NOT NULL,
                hour_start_ts REAL NOT NULL,
                hour_start_iso TEXT NOT NULL,
                heating_kwh REAL,
                cooling_kwh REAL,
                dry_kwh REAL,
                fan_kwh REAL,
                auto_kwh REAL,
                other_kwh REAL,
                total_kwh REAL,
                coverage_percent REAL,  -- UsageDisclaimerPercentages from the report
                fetched_at REAL,
                PRIMARY KEY (device_id, hour_start_ts)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _creds_present() -> bool:
    return bool(os.environ.get("MELCLOUD_USER") and os.environ.get("MELCLOUD_PASS"))


async def _login(session: aiohttp.ClientSession) -> str:
    user = os.environ["MELCLOUD_USER"]
    pw = os.environ["MELCLOUD_PASS"]
    return await pymelcloud.login(user, pw, session=session)


async def _fetch_all(session: aiohttp.ClientSession, token: str) -> List[Dict[str, Any]]:
    devices = await pymelcloud.get_devices(token, session=session)
    readings: List[Dict[str, Any]] = []
    for kind, devs in devices.items():
        for d in devs:
            try:
                await d.update()
            except Exception as e:  # noqa: BLE001
                print(f"⚠️  MELCloud device {d.name!r} update failed: {e}")
                continue
            dev = getattr(d, "_device_conf", {}).get("Device", {}) or {}

            current_wh = dev.get("CurrentEnergyConsumed")
            total_kwh = (
                float(current_wh) / 1000.0 if current_wh is not None
                else getattr(d, "total_energy_consumed", None)
            )

            last_seen = getattr(d, "last_seen", None)
            if isinstance(last_seen, datetime):
                last_iso = last_seen.astimezone(timezone.utc).isoformat()
            else:
                last_iso = str(last_seen) if last_seen else None

            readings.append({
                "device_id": d.device_id,
                "building_id": d.building_id,
                "serial": getattr(d, "serial", None),
                "mac": getattr(d, "mac", None),
                "kind": kind,
                "name": d.name,
                "power": 1 if getattr(d, "power", False) else 0,
                "operation_mode": str(getattr(d, "operation_mode", "") or "") or None,
                "room_temperature": getattr(d, "room_temperature", None),
                "target_temperature": getattr(d, "target_temperature", None),
                "outdoor_temperature": dev.get("OutdoorTemperature"),
                "fan_speed": str(getattr(d, "fan_speed", "") or "") or None,
                "vane_horizontal": str(getattr(d, "vane_horizontal", "") or "") or None,
                "vane_vertical": str(getattr(d, "vane_vertical", "") or "") or None,
                "total_energy_consumed_kwh": total_kwh,
                "wifi_signal_dbm": dev.get("WifiSignalStrength"),
                "last_communication": last_iso,
            })
    return readings


async def _poll_async(db_path: str) -> List[Dict[str, Any]]:
    global _cached_token, _token_acquired_at

    ensure_melcloud_tables(db_path)
    now_ts = time.time()
    now_iso = datetime.now().isoformat()

    async with aiohttp.ClientSession() as session:
        # Re-use cached token if fresh; otherwise login
        with _token_lock:
            token = _cached_token
        if not token:
            token = await _login(session)
            with _token_lock:
                _cached_token = token
                _token_acquired_at = now_ts

        try:
            readings = await _fetch_all(session, token)
        except Exception as e:  # token probably expired
            msg = str(e).lower()
            if "401" in msg or "unauth" in msg or "forbidden" in msg or "403" in msg:
                token = await _login(session)
                with _token_lock:
                    _cached_token = token
                    _token_acquired_at = now_ts
                readings = await _fetch_all(session, token)
            else:
                raise

    # Persist
    conn = sqlite3.connect(db_path)
    try:
        for r in readings:
            conn.execute(
                """
                INSERT INTO melcloud_devices
                (device_id, building_id, serial, mac, kind, name, enabled, discovered_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    mac = COALESCE(excluded.mac, melcloud_devices.mac),
                    name = excluded.name,
                    kind = excluded.kind,
                    serial = COALESCE(excluded.serial, melcloud_devices.serial),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    r["device_id"], r["building_id"], r["serial"], r["mac"],
                    r["kind"], r["name"], now_ts, now_ts,
                ),
            )
            conn.execute(
                """
                INSERT INTO melcloud_readings (
                    timestamp, datetime, device_id, name, power, operation_mode,
                    room_temperature, target_temperature, outdoor_temperature,
                    fan_speed, vane_horizontal, vane_vertical,
                    total_energy_consumed_kwh, wifi_signal_dbm, last_communication
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts, now_iso, r["device_id"], r["name"], r["power"],
                    r["operation_mode"], r["room_temperature"], r["target_temperature"],
                    r["outdoor_temperature"], r["fan_speed"], r["vane_horizontal"],
                    r["vane_vertical"], r["total_energy_consumed_kwh"],
                    r["wifi_signal_dbm"], r["last_communication"],
                ),
            )
            r["timestamp"] = now_ts
            r["datetime"] = now_iso
        conn.commit()
    finally:
        conn.close()

    return readings


def poll_melcloud(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Synchronous wrapper: fetch, persist, return latest readings.

    Returns [] if credentials are missing. Raises on other errors so the caller
    (background thread) can log and back off.
    """
    if not _creds_present():
        return []
    return asyncio.run(_poll_async(db_path))


def list_devices_with_latest(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Return each known device joined with its most-recent reading."""
    ensure_melcloud_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.device_id, d.name, d.kind, d.mac, d.serial, d.enabled,
                   d.last_seen_at,
                   r.timestamp, r.datetime, r.power, r.operation_mode,
                   r.room_temperature, r.target_temperature, r.outdoor_temperature,
                   r.fan_speed, r.vane_horizontal, r.vane_vertical,
                   r.total_energy_consumed_kwh, r.wifi_signal_dbm,
                   r.last_communication
            FROM melcloud_devices d
            LEFT JOIN melcloud_readings r ON r.id = (
                SELECT id FROM melcloud_readings
                WHERE device_id = d.device_id
                ORDER BY timestamp DESC LIMIT 1
            )
            ORDER BY d.name
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "device_id": r[0], "name": r[1], "kind": r[2], "mac": r[3],
            "serial": r[4], "enabled": bool(r[5]), "last_seen_at": r[6],
            "timestamp": r[7], "datetime": r[8],
            "power": bool(r[9]) if r[9] is not None else None,
            "operation_mode": r[10], "room_temperature": r[11],
            "target_temperature": r[12], "outdoor_temperature": r[13],
            "fan_speed": r[14], "vane_horizontal": r[15], "vane_vertical": r[16],
            "total_energy_consumed_kwh": r[17], "wifi_signal_dbm": r[18],
            "last_communication": r[19],
        }
        for r in rows
    ]


async def _fetch_energy_segment(
    session: aiohttp.ClientSession,
    token: str,
    device_id: int,
    from_local: datetime,
    to_local: datetime,
) -> Dict[str, Any]:
    """Fetch one EnergyCost/Report segment. Dates are local (MELCloud TZ)."""
    body = {
        "DeviceID": device_id,
        "FromDate": from_local.strftime("%Y-%m-%dT00:00:00"),
        "ToDate": to_local.strftime("%Y-%m-%dT23:59:59"),
        "UseCurrency": False,
    }
    headers = {
        "X-MitsContextKey": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with session.post(ENERGY_REPORT_URL, headers=headers, data=json.dumps(body)) as r:
        r.raise_for_status()
        return await r.json()


def _upsert_energy_hourly(
    db_path: str,
    device_id: int,
    report: Dict[str, Any],
    segment_start_local: datetime,
) -> int:
    """Write hourly rows from a LabelType=0 report. Returns rows upserted."""
    label_type = report.get("LabelType")
    labels = report.get("Labels") or []
    if label_type != 0 or not labels:
        return 0  # daily granularity — not what we want

    coverage_str = report.get("UsageDisclaimerPercentages") or ""
    try:
        coverage = float(coverage_str.rstrip("%")) if coverage_str else None
    except ValueError:
        coverage = None

    heating = report.get("Heating") or []
    cooling = report.get("Cooling") or []
    dry = report.get("Dry") or []
    fan = report.get("Fan") or []
    auto = report.get("Auto") or []
    other = report.get("Other") or []
    now_ts = time.time()

    conn = sqlite3.connect(db_path)
    try:
        # MELCloud returns hours counted from the FromDate midnight (local).
        # Labels repeat 0..23 per day; their position in the list is the offset in hours.
        for i, _label in enumerate(labels):
            hour_dt = segment_start_local + timedelta(hours=i)
            # Treat as UTC-naive local timestamp — cheap and consistent with
            # the rest of the DB which stores local ISO datetimes.
            hour_ts = hour_dt.timestamp()
            h = heating[i] if i < len(heating) else 0.0
            c = cooling[i] if i < len(cooling) else 0.0
            dr = dry[i] if i < len(dry) else 0.0
            f = fan[i] if i < len(fan) else 0.0
            a = auto[i] if i < len(auto) else 0.0
            o = other[i] if i < len(other) else 0.0
            total = (h or 0) + (c or 0) + (dr or 0) + (f or 0) + (a or 0) + (o or 0)
            conn.execute(
                """
                INSERT INTO melcloud_energy_hourly
                (device_id, hour_start_ts, hour_start_iso,
                 heating_kwh, cooling_kwh, dry_kwh, fan_kwh, auto_kwh, other_kwh,
                 total_kwh, coverage_percent, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, hour_start_ts) DO UPDATE SET
                    heating_kwh=excluded.heating_kwh,
                    cooling_kwh=excluded.cooling_kwh,
                    dry_kwh=excluded.dry_kwh,
                    fan_kwh=excluded.fan_kwh,
                    auto_kwh=excluded.auto_kwh,
                    other_kwh=excluded.other_kwh,
                    total_kwh=excluded.total_kwh,
                    coverage_percent=excluded.coverage_percent,
                    fetched_at=excluded.fetched_at
                """,
                (device_id, hour_ts, hour_dt.isoformat(),
                 h, c, dr, f, a, o, total, coverage, now_ts),
            )
        conn.commit()
        return len(labels)
    finally:
        conn.close()


async def _refresh_energy_for_device(
    session: aiohttp.ClientSession,
    token: str,
    device_id: int,
    days_back: int,
) -> int:
    """Fetch energy reports in 48-hour segments and upsert hourly rows.

    Returns total hourly rows upserted.
    """
    today_local = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total_rows = 0
    remaining = days_back
    segment_end = today_local + timedelta(days=1) - timedelta(seconds=1)
    while remaining > 0:
        span_days = min(ENERGY_HOURLY_SEGMENT_HOURS // 24, remaining)
        segment_start = (segment_end - timedelta(days=span_days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        try:
            report = await _fetch_energy_segment(
                session, token, device_id, segment_start, segment_end,
            )
            total_rows += _upsert_energy_hourly(DB_PATH, device_id, report, segment_start)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  MELCloud energy fetch failed for {device_id} "
                  f"{segment_start.date()}..{segment_end.date()}: {e}")
        segment_end = segment_start - timedelta(seconds=1)
        remaining -= span_days
    return total_rows


async def _refresh_energy_all_async(db_path: str, days_back: int) -> Dict[int, int]:
    global _cached_token, _token_acquired_at
    if not _creds_present():
        return {}
    ensure_melcloud_tables(db_path)

    async with aiohttp.ClientSession() as session:
        with _token_lock:
            token = _cached_token
        if not token:
            token = await _login(session)
            with _token_lock:
                _cached_token = token
                _token_acquired_at = time.time()

        # Fetch device list from DB (cheaper than hitting MELCloud for it)
        conn = sqlite3.connect(db_path)
        try:
            device_ids = [row[0] for row in conn.execute(
                "SELECT device_id FROM melcloud_devices WHERE enabled = 1"
            )]
        finally:
            conn.close()

        if not device_ids:
            # First run — grab devices from API
            devs = await pymelcloud.get_devices(token, session=session)
            device_ids = [d.device_id for kind in devs.values() for d in kind]

        results = {}
        for dev_id in device_ids:
            rows = await _refresh_energy_for_device(session, token, dev_id, days_back)
            results[dev_id] = rows
        return results


def refresh_energy_history(days_back: int = 7, db_path: str = DB_PATH) -> Dict[int, int]:
    """Backfill hourly energy data for the last `days_back` days.

    Safe to call on startup (backfill) and periodically (refresh recent hours).
    Returns mapping of device_id → rows upserted.
    """
    return asyncio.run(_refresh_energy_all_async(db_path, days_back))


def estimate_current_power_w(device_id: int, db_path: str = DB_PATH, window_seconds: int = 1200) -> Optional[float]:
    """Estimate recent average power (W) from the delta of
    total_energy_consumed_kwh over the last `window_seconds`.

    Returns None if we don't have two readings within the window with
    monotonically increasing counters.
    """
    ensure_melcloud_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT timestamp, total_energy_consumed_kwh
            FROM melcloud_readings
            WHERE device_id = ? AND total_energy_consumed_kwh IS NOT NULL
              AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (device_id, time.time() - window_seconds),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < 2:
        return None
    t0, e0 = rows[0]
    t1, e1 = rows[-1]
    if t1 <= t0 or e1 < e0:
        return None
    dt_seconds = t1 - t0
    dkwh = e1 - e0
    # kWh / seconds × 3600000 = W (average over window)
    return (dkwh * 3_600_000.0) / dt_seconds


if __name__ == "__main__":
    # Load .env for CLI runs
    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    readings = poll_melcloud()
    if not readings:
        print("No readings (check MELCLOUD_USER / MELCLOUD_PASS)")
    else:
        for r in readings:
            print(
                f"{r['name']:20s} {r['operation_mode']:5s} "
                f"room={r['room_temperature']}°C  set={r['target_temperature']}°C  "
                f"outdoor={r['outdoor_temperature']}°C  "
                f"kWh={r['total_energy_consumed_kwh']:.1f}  "
                f"wifi={r['wifi_signal_dbm']}dBm  power={'ON' if r['power'] else 'OFF'}"
            )
