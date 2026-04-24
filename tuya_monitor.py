#!/usr/bin/env python3
"""Tuya / Nedis smart plug polling and persistence via tinytuya.

This module complements the existing Kasa integration:

  - `devices.json` from `tinytuya wizard` provides Tuya cloud-linked devices
    with their local keys for live polling.
  - optional `tuya_devices.json` can add local aliases, DPS overrides, or
    placeholder candidates before the real Tuya key is available.
  - readings are stored in the shared `p1_data.db` SQLite database.

The monitored Nedis plugs are typically Tuya-based and speak local TCP on
port 6668. Without a local key we can identify the device, but not decrypt
live power telemetry. This module keeps the app ready for that final setup
step instead of failing hard.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import tinytuya
except ModuleNotFoundError:  # pragma: no cover - handled gracefully at runtime
    tinytuya = None


DEFAULT_DEVICE_FILE = Path("devices.json")
DEFAULT_OVERRIDE_FILE = Path("tuya_devices.json")
READ_TIMEOUT_SECONDS = 5
DEFAULT_VERSION = 3.3


def is_available() -> bool:
    return tinytuya is not None


def ensure_tuya_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tuya_plugs (
                device_uid TEXT PRIMARY KEY,
                device_id TEXT,
                ip TEXT,
                mac TEXT,
                name TEXT,
                version REAL,
                local_key_present INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                discovered_at REAL,
                last_seen_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tuya_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                datetime TEXT NOT NULL,
                device_uid TEXT NOT NULL,
                device_id TEXT,
                ip TEXT,
                mac TEXT,
                name TEXT,
                power_w REAL,
                voltage_v REAL,
                current_a REAL,
                total_kwh REAL,
                is_on INTEGER,
                dps_json TEXT,
                raw_status_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tuya_readings_uid_ts ON tuya_readings (device_uid, timestamp)"
        )
        conn.commit()
    finally:
        conn.close()


def _normalize_mac(mac: Any) -> str:
    if mac is None:
        return ""
    text = str(mac).strip().lower().replace("-", ":")
    parts = [part.zfill(2) for part in text.split(":") if part]
    if len(parts) == 6:
        return ":".join(parts)
    return text


def _to_float(value: Any, default: float = DEFAULT_VERSION) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _read_json_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("devices"), list):
            return [item for item in payload["devices"] if isinstance(item, dict)]
        return [payload]
    return []


def _device_uid(device_id: str | None, mac: str | None, ip: str | None) -> str:
    mac = _normalize_mac(mac)
    if mac:
        return f"mac:{mac}"
    if device_id:
        return f"id:{device_id}"
    if ip:
        return f"ip:{ip}"
    return "unknown"


def _find_override(
    override_by_uid: Dict[str, Dict[str, Any]],
    device_id: str | None,
    mac: str | None,
    ip: str | None,
) -> Dict[str, Any]:
    keys = [
        _device_uid(device_id, mac, ip),
        _device_uid(device_id, mac, None),
        _device_uid(device_id, None, ip),
    ]
    for key in keys:
        if key in override_by_uid:
            return dict(override_by_uid[key])
    return {}


def load_devices(
    device_file: str | Path | None = None,
    override_file: str | Path | None = None,
) -> List[Dict[str, Any]]:
    """Load Tuya devices from tinytuya's devices.json plus optional overrides."""
    device_path = Path(device_file or DEFAULT_DEVICE_FILE)
    override_path = Path(override_file or DEFAULT_OVERRIDE_FILE)

    override_by_uid: Dict[str, Dict[str, Any]] = {}
    for raw in _read_json_file(override_path):
        device_id = raw.get("device_id") or raw.get("id") or raw.get("gwId")
        mac = _normalize_mac(raw.get("mac"))
        ip = (raw.get("ip") or "").strip()
        uid = _device_uid(device_id, mac, ip)
        item = dict(raw)
        item["device_uid"] = uid
        item["device_id"] = device_id
        item["mac"] = mac
        item["ip"] = ip
        override_by_uid[uid] = item

    devices_by_uid: Dict[str, Dict[str, Any]] = {}

    # Start with cloud-linked tinytuya devices.
    for raw in _read_json_file(device_path):
        device_id = raw.get("device_id") or raw.get("id") or raw.get("gwId")
        mac = _normalize_mac(raw.get("mac"))
        ip = (raw.get("ip") or "").strip()
        uid = _device_uid(device_id, mac, ip)
        override = _find_override(override_by_uid, device_id, mac, ip)
        devices_by_uid[uid] = {
            "device_uid": uid,
            "device_id": device_id,
            "ip": override.get("ip") or ip,
            "mac": override.get("mac") or mac,
            "name": override.get("name") or raw.get("name") or "",
            "local_key": override.get("local_key") or override.get("key") or raw.get("local_key") or raw.get("key") or "",
            "version": _to_float(override.get("version") or raw.get("version") or raw.get("ver")),
            "enabled": _to_bool(override.get("enabled"), True),
            "notes": override.get("notes") or raw.get("notes") or "",
            "switch_dps": override.get("switch_dps"),
            "current_dps": override.get("current_dps"),
            "current_scale": _to_float(override.get("current_scale"), 1000.0),
            "power_dps": override.get("power_dps"),
            "power_scale": _to_float(override.get("power_scale"), 10.0),
            "voltage_dps": override.get("voltage_dps"),
            "voltage_scale": _to_float(override.get("voltage_scale"), 10.0),
            "energy_dps": override.get("energy_dps"),
            "energy_scale": _to_float(override.get("energy_scale"), 1000.0),
        }

    # Add placeholder-only local candidates not yet present in devices.json.
    for uid, raw in override_by_uid.items():
        devices_by_uid.setdefault(
            uid,
            {
                "device_uid": uid,
                "device_id": raw.get("device_id") or raw.get("id") or raw.get("gwId"),
                "ip": raw.get("ip") or "",
                "mac": _normalize_mac(raw.get("mac")),
                "name": raw.get("name") or "",
                "local_key": raw.get("local_key") or raw.get("key") or "",
                "version": _to_float(raw.get("version")),
                "enabled": _to_bool(raw.get("enabled"), True),
                "notes": raw.get("notes") or "",
                "switch_dps": raw.get("switch_dps"),
                "current_dps": raw.get("current_dps"),
                "current_scale": _to_float(raw.get("current_scale"), 1000.0),
                "power_dps": raw.get("power_dps"),
                "power_scale": _to_float(raw.get("power_scale"), 10.0),
                "voltage_dps": raw.get("voltage_dps"),
                "voltage_scale": _to_float(raw.get("voltage_scale"), 10.0),
                "energy_dps": raw.get("energy_dps"),
                "energy_scale": _to_float(raw.get("energy_scale"), 1000.0),
            },
        )

    return sorted(
        devices_by_uid.values(),
        key=lambda item: (
            (item.get("name") or "").lower(),
            item.get("ip") or "",
            item.get("device_uid") or "",
        ),
    )


def upsert_plugs(db_path: str, plugs: Iterable[Dict[str, Any]]) -> None:
    ensure_tuya_tables(db_path)
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        for p in plugs:
            conn.execute(
                """
                INSERT INTO tuya_plugs
                (device_uid, device_id, ip, mac, name, version, local_key_present,
                 enabled, notes, discovered_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_uid) DO UPDATE SET
                    device_id=excluded.device_id,
                    ip=excluded.ip,
                    mac=excluded.mac,
                    name=CASE
                        WHEN excluded.name IS NOT NULL AND excluded.name != ''
                        THEN excluded.name
                        ELSE tuya_plugs.name
                    END,
                    version=excluded.version,
                    local_key_present=excluded.local_key_present,
                    enabled=excluded.enabled,
                    notes=excluded.notes
                """,
                (
                    p["device_uid"],
                    p.get("device_id"),
                    p.get("ip"),
                    p.get("mac"),
                    p.get("name"),
                    p.get("version"),
                    1 if p.get("local_key") else 0,
                    1 if p.get("enabled", True) else 0,
                    p.get("notes"),
                    now,
                    p.get("last_seen_at"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def list_plugs(db_path: str, only_enabled: bool = False) -> List[Dict[str, Any]]:
    ensure_tuya_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT device_uid, device_id, ip, mac, name, version, local_key_present, "
            "enabled, notes, discovered_at, last_seen_at "
            "FROM tuya_plugs"
        )
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY COALESCE(NULLIF(name, ''), ip, device_uid)"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [
        {
            "device_uid": row[0],
            "device_id": row[1],
            "ip": row[2],
            "mac": row[3],
            "name": row[4],
            "version": row[5],
            "local_key_present": bool(row[6]),
            "enabled": bool(row[7]),
            "notes": row[8],
            "discovered_at": row[9],
            "last_seen_at": row[10],
        }
        for row in rows
    ]


def _as_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pick_scaled_metric(
    dps: Dict[str, Any],
    override_key: Any,
    override_scale: float,
    default_candidates: Iterable[tuple[str, float]],
) -> Optional[float]:
    if override_key is not None:
        value = _as_number(dps.get(str(override_key)))
        if value is not None:
            return value / override_scale
    for key, scale in default_candidates:
        value = _as_number(dps.get(key))
        if value is not None:
            return value / scale
    return None


def _pick_switch_state(dps: Dict[str, Any], override_key: Any) -> Optional[bool]:
    keys = [str(override_key)] if override_key is not None else []
    keys.extend(["1", "20"])
    for key in keys:
        if key in dps:
            value = dps[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in {"true", "false", "0", "1", "on", "off"}:
                return text in {"true", "1", "on"}
    return None


def _build_client(device: Dict[str, Any]):
    client = tinytuya.OutletDevice(
        device.get("device_id") or device["device_uid"],
        address=device.get("ip") or None,
        local_key=device.get("local_key") or "",
        version=_to_float(device.get("version")),
    )
    client.set_version(_to_float(device.get("version")))
    client.set_socketTimeout(READ_TIMEOUT_SECONDS)
    client.set_socketPersistent(False)
    return client


def _poll_device(device: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not is_available():
        return None
    if not device.get("local_key") or not device.get("device_id") or not device.get("ip"):
        return None

    try:
        status = _build_client(device).status()
    except Exception:
        return None

    if not isinstance(status, dict):
        return None

    dps = status.get("dps") if isinstance(status.get("dps"), dict) else {}
    power_w = _pick_scaled_metric(
        dps,
        device.get("power_dps"),
        _to_float(device.get("power_scale"), 10.0),
        [("19", 10.0), ("103", 10.0)],
    )
    voltage_v = _pick_scaled_metric(
        dps,
        device.get("voltage_dps"),
        _to_float(device.get("voltage_scale"), 10.0),
        [("20", 10.0), ("104", 10.0)],
    )
    current_a = _pick_scaled_metric(
        dps,
        device.get("current_dps"),
        _to_float(device.get("current_scale"), 1000.0),
        [("18", 1000.0), ("101", 1000.0)],
    )
    total_kwh = _pick_scaled_metric(
        dps,
        device.get("energy_dps"),
        _to_float(device.get("energy_scale"), 1000.0),
        [("17", 1000.0)],
    )

    return {
        "device_uid": device["device_uid"],
        "device_id": device.get("device_id"),
        "ip": device.get("ip"),
        "mac": device.get("mac"),
        "name": device.get("name") or device.get("device_id") or device["device_uid"],
        "power_w": power_w,
        "voltage_v": voltage_v,
        "current_a": current_a,
        "total_kwh": total_kwh,
        "is_on": _pick_switch_state(dps, device.get("switch_dps")),
        "dps_json": json.dumps(dps, ensure_ascii=True, sort_keys=True),
        "raw_status_json": json.dumps(status, ensure_ascii=True, sort_keys=True),
    }


def poll_plugs(
    db_path: str,
    device_file: str | Path | None = None,
    override_file: str | Path | None = None,
) -> List[Dict[str, Any]]:
    """Poll all configured Tuya devices with local keys and persist readings."""
    devices = load_devices(device_file=device_file, override_file=override_file)
    upsert_plugs(db_path, devices)

    active_devices = [
        device
        for device in devices
        if device.get("enabled", True)
        and device.get("local_key")
        and device.get("device_id")
        and device.get("ip")
    ]
    if not active_devices or not is_available():
        return []

    readings = []
    for device in active_devices:
        reading = _poll_device(device)
        if reading:
            readings.append(reading)
    if not readings:
        return []

    now_ts = time.time()
    now_iso = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for reading in readings:
            conn.execute(
                """
                INSERT INTO tuya_readings
                (timestamp, datetime, device_uid, device_id, ip, mac, name, power_w,
                 voltage_v, current_a, total_kwh, is_on, dps_json, raw_status_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts,
                    now_iso,
                    reading["device_uid"],
                    reading.get("device_id"),
                    reading.get("ip"),
                    reading.get("mac"),
                    reading.get("name"),
                    reading.get("power_w"),
                    reading.get("voltage_v"),
                    reading.get("current_a"),
                    reading.get("total_kwh"),
                    1 if reading.get("is_on") else 0 if reading.get("is_on") is not None else None,
                    reading.get("dps_json"),
                    reading.get("raw_status_json"),
                ),
            )
            conn.execute(
                "UPDATE tuya_plugs SET last_seen_at = ? WHERE device_uid = ?",
                (now_ts, reading["device_uid"]),
            )
        conn.commit()
    finally:
        conn.close()

    for reading in readings:
        reading["timestamp"] = now_ts
        reading["datetime"] = now_iso
    return readings


def get_latest_readings(db_path: str) -> List[Dict[str, Any]]:
    ensure_tuya_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.device_uid, p.device_id, p.ip, p.mac, p.name, p.version,
                   p.local_key_present, p.enabled, p.notes, p.last_seen_at,
                   r.timestamp, r.datetime, r.power_w, r.voltage_v, r.current_a,
                   r.total_kwh, r.is_on, r.dps_json
            FROM tuya_plugs p
            LEFT JOIN tuya_readings r ON r.id = (
                SELECT id FROM tuya_readings WHERE device_uid = p.device_uid
                ORDER BY timestamp DESC LIMIT 1
            )
            ORDER BY COALESCE(NULLIF(p.name, ''), p.ip, p.device_uid)
            """
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        has_key = bool(row[6])
        timestamp = row[10]
        status_note = ""
        if not has_key:
            status_note = "Needs Tuya local key from tinytuya wizard"
        elif timestamp is None:
            status_note = "Configured but no live response yet"
        result.append(
            {
                "device_uid": row[0],
                "device_id": row[1],
                "ip": row[2],
                "mac": row[3],
                "name": row[4],
                "version": row[5],
                "local_key_present": has_key,
                "enabled": bool(row[7]),
                "notes": row[8],
                "last_seen_at": row[9],
                "timestamp": timestamp,
                "datetime": row[11],
                "power_w": row[12],
                "voltage_v": row[13],
                "current_a": row[14],
                "total_kwh": row[15],
                "is_on": bool(row[16]) if row[16] is not None else None,
                "dps_json": row[17],
                "status_note": status_note,
                "needs_setup": not has_key,
            }
        )
    return result


def get_history(
    db_path: str,
    start_ts: float,
    end_ts: float,
    device_uid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ensure_tuya_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        if device_uid:
            rows = conn.execute(
                """
                SELECT timestamp, datetime, device_uid, device_id, ip, mac, name,
                       power_w, voltage_v, current_a, total_kwh, is_on, dps_json
                FROM tuya_readings
                WHERE device_uid = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
                LIMIT 20000
                """,
                (device_uid, start_ts, end_ts),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT timestamp, datetime, device_uid, device_id, ip, mac, name,
                       power_w, voltage_v, current_a, total_kwh, is_on, dps_json
                FROM tuya_readings
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
            "timestamp": row[0],
            "datetime": row[1],
            "device_uid": row[2],
            "device_id": row[3],
            "ip": row[4],
            "mac": row[5],
            "name": row[6],
            "power_w": row[7],
            "voltage_v": row[8],
            "current_a": row[9],
            "total_kwh": row[10],
            "is_on": bool(row[11]) if row[11] is not None else None,
            "dps_json": row[12],
        }
        for row in rows
    ]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "poll":
        rows = poll_plugs("p1_data.db")
        print(f"Polled {len(rows)} Tuya plug(s)")
        for row in rows:
            print(
                f"{row['name'] or row['device_uid']:25} "
                f"{(row.get('power_w') or 0):7.1f}W  "
                f"{(row.get('voltage_v') or 0):6.1f}V  "
                f"{(row.get('current_a') or 0):5.2f}A"
            )
    else:
        devices = load_devices()
        print(f"Configured {len(devices)} Tuya device(s)")
        for device in devices:
            print(
                f"{device.get('name') or device['device_uid']:25} "
                f"{device.get('ip') or '-':15} "
                f"{device.get('mac') or '-':17} "
                f"key={'yes' if device.get('local_key') else 'no'}"
            )
