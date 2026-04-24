#!/usr/bin/env python3
"""Build reusable appliance profiles from smart-plug history.

Profiles are intentionally based on sampled power_w instead of plug-reported
total_kwh because some plugs reset or wrap their counters. The resulting JSON is
safe to keep after moving a plug to another appliance.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROFILE_SNAPSHOTS_TABLE = "appliance_profile_snapshots"
PLUG_ASSIGNMENTS_TABLE = "plug_assignments"

SOURCES = [
    {
        "source": "kasa",
        "table": "kasa_readings",
        "id_col": "mac",
        "name_col": "alias",
    },
    {
        "source": "tuya",
        "table": "tuya_readings",
        "id_col": "device_uid",
        "name_col": "name",
    },
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_source_config(source_name: str) -> Dict[str, str]:
    for source in SOURCES:
        if source["source"] == source_name:
            return source
    known = ", ".join(source["source"] for source in SOURCES)
    raise ValueError(f"Unknown source {source_name!r}; expected one of: {known}")


def percentile(values: Iterable[float], percent: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = (len(vals) - 1) * percent / 100
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return float(vals[lo])
    return float(vals[lo] * (hi - idx) + vals[hi] * (idx - lo))


def median(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else None


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def ensure_tracking_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROFILE_SNAPSHOTS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            device_id TEXT NOT NULL,
            appliance_label TEXT NOT NULL,
            profile_start TEXT,
            profile_end TEXT,
            profile_json TEXT NOT NULL,
            notes TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PLUG_ASSIGNMENTS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            device_id TEXT NOT NULL,
            appliance_label TEXT NOT NULL,
            assigned_from TEXT NOT NULL,
            assigned_to TEXT,
            profile_snapshot_id INTEGER,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{PROFILE_SNAPSHOTS_TABLE}_device
        ON {PROFILE_SNAPSHOTS_TABLE} (source, device_id, created_at)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{PLUG_ASSIGNMENTS_TABLE}_device
        ON {PLUG_ASSIGNMENTS_TABLE} (source, device_id, assigned_from, assigned_to)
        """
    )
    conn.commit()


def infer_run_threshold(powers: List[float]) -> float:
    """Pick a pragmatic on/off threshold for common household appliances."""
    p75 = percentile(powers, 75) or 0
    p95 = percentile(powers, 95) or 0

    if p75 < 10 <= p95:
        return 10.0
    if p95 >= 20:
        return 20.0
    if p95 >= 5:
        return max(3.0, p95 / 2)
    return 1.0


def integrate_kwh(rows: List[sqlite3.Row], max_gap_seconds: float) -> Dict[str, float]:
    kwh = 0.0
    covered_seconds = 0.0
    for prev, curr in zip(rows, rows[1:]):
        dt = float(curr["timestamp"]) - float(prev["timestamp"])
        if 0 < dt <= max_gap_seconds:
            avg_w = (float(prev["power_w"] or 0) + float(curr["power_w"] or 0)) / 2
            kwh += avg_w * dt / 3_600_000
            covered_seconds += dt
    return {"kwh": kwh, "covered_seconds": covered_seconds}


def build_segments(
    rows: List[sqlite3.Row],
    threshold_w: float,
    max_gap_seconds: float,
) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for row in rows:
        power_w = float(row["power_w"] or 0)
        state = power_w >= threshold_w

        if current is None:
            current = {
                "state": state,
                "start_ts": float(row["timestamp"]),
                "end_ts": float(row["timestamp"]),
                "start": row["datetime"],
                "end": row["datetime"],
                "powers": [power_w],
            }
            continue

        gap = float(row["timestamp"]) - current["end_ts"]
        if gap > max_gap_seconds or state != current["state"]:
            segments.append(current)
            current = {
                "state": state,
                "start_ts": float(row["timestamp"]),
                "end_ts": float(row["timestamp"]),
                "start": row["datetime"],
                "end": row["datetime"],
                "powers": [power_w],
            }
            continue

        current["end_ts"] = float(row["timestamp"])
        current["end"] = row["datetime"]
        current["powers"].append(power_w)

    if current is not None:
        segments.append(current)

    return segments


def hourly_profile(rows: List[sqlite3.Row], max_gap_seconds: float) -> Dict[str, float]:
    buckets: Dict[str, Dict[str, float]] = defaultdict(lambda: {"kwh": 0.0, "seconds": 0.0})
    for prev, curr in zip(rows, rows[1:]):
        dt = float(curr["timestamp"]) - float(prev["timestamp"])
        if 0 < dt <= max_gap_seconds:
            avg_w = (float(prev["power_w"] or 0) + float(curr["power_w"] or 0)) / 2
            hour = datetime.fromtimestamp((float(prev["timestamp"]) + float(curr["timestamp"])) / 2).strftime("%H")
            buckets[hour]["kwh"] += avg_w * dt / 3_600_000
            buckets[hour]["seconds"] += dt

    return {
        hour: values["kwh"] * 1000 / (values["seconds"] / 3600)
        for hour, values in sorted(buckets.items())
        if values["seconds"] > 0
    }


def daily_profile(rows: List[sqlite3.Row], max_gap_seconds: float) -> Dict[str, float]:
    buckets: Dict[str, Dict[str, float]] = defaultdict(lambda: {"kwh": 0.0, "seconds": 0.0})
    for prev, curr in zip(rows, rows[1:]):
        dt = float(curr["timestamp"]) - float(prev["timestamp"])
        if 0 < dt <= max_gap_seconds:
            avg_w = (float(prev["power_w"] or 0) + float(curr["power_w"] or 0)) / 2
            day = datetime.fromtimestamp((float(prev["timestamp"]) + float(curr["timestamp"])) / 2).strftime("%Y-%m-%d")
            buckets[day]["kwh"] += avg_w * dt / 3_600_000
            buckets[day]["seconds"] += dt

    return {
        day: values["kwh"] / (values["seconds"] / 86_400)
        for day, values in sorted(buckets.items())
        if values["seconds"] > 0
    }


def profile_device(
    rows: List[sqlite3.Row],
    source: str,
    device_id: str,
    label: str,
    max_gap_seconds: float,
    threshold_w: Optional[float],
) -> Dict[str, Any]:
    powers = [float(row["power_w"] or 0) for row in rows]
    first = rows[0]
    last = rows[-1]
    elapsed_seconds = float(last["timestamp"]) - float(first["timestamp"])
    integrated = integrate_kwh(rows, max_gap_seconds)
    threshold = threshold_w if threshold_w is not None else infer_run_threshold(powers)
    segments = build_segments(rows, threshold, max_gap_seconds)
    on_segments = [seg for seg in segments if seg["state"]]
    off_segments = [seg for seg in segments if not seg["state"]]
    on_seconds = sum(max(0.0, seg["end_ts"] - seg["start_ts"]) for seg in on_segments)
    off_seconds = sum(max(0.0, seg["end_ts"] - seg["start_ts"]) for seg in off_segments)
    on_powers = [power for power in powers if power >= threshold]
    off_powers = [power for power in powers if power < threshold]
    on_minutes = [
        (seg["end_ts"] - seg["start_ts"]) / 60
        for seg in on_segments
        if seg["end_ts"] - seg["start_ts"] >= 60
    ]
    off_minutes = [
        (seg["end_ts"] - seg["start_ts"]) / 60
        for seg in off_segments
        if seg["end_ts"] - seg["start_ts"] >= 60
    ]

    covered_hours = integrated["covered_seconds"] / 3600
    avg_w = integrated["kwh"] * 1000 / covered_hours if covered_hours else 0.0
    scaled_daily_kwh = (
        integrated["kwh"] / (integrated["covered_seconds"] / 86_400)
        if integrated["covered_seconds"]
        else 0.0
    )

    return {
        "source": source,
        "device_id": device_id,
        "label": label,
        "start": first["datetime"],
        "end": last["datetime"],
        "rows": len(rows),
        "elapsed_hours": elapsed_seconds / 3600 if elapsed_seconds > 0 else 0,
        "covered_hours": covered_hours,
        "coverage_pct": (integrated["covered_seconds"] / elapsed_seconds * 100) if elapsed_seconds > 0 else 0,
        "integrated_kwh": integrated["kwh"],
        "avg_w": avg_w,
        "scaled_daily_kwh": scaled_daily_kwh,
        "scaled_annual_kwh": scaled_daily_kwh * 365,
        "threshold_w": threshold,
        "duty_cycle_pct": (on_seconds / (on_seconds + off_seconds) * 100) if (on_seconds + off_seconds) else 0,
        "cycle_count": len(on_segments),
        "on_duration_min_median": median(on_minutes),
        "on_duration_min_mean": float(statistics.mean(on_minutes)) if on_minutes else None,
        "off_duration_min_median": median(off_minutes),
        "off_duration_min_mean": float(statistics.mean(off_minutes)) if off_minutes else None,
        "power_w": {
            "p05": percentile(powers, 5),
            "p25": percentile(powers, 25),
            "median": percentile(powers, 50),
            "p75": percentile(powers, 75),
            "p95": percentile(powers, 95),
            "p99": percentile(powers, 99),
            "max": max(powers) if powers else None,
            "running_median": percentile(on_powers, 50),
            "running_p95": percentile(on_powers, 95),
            "standby_median": percentile(off_powers, 50),
            "standby_p95": percentile(off_powers, 95),
        },
        "hourly_avg_w": hourly_profile(rows, max_gap_seconds),
        "daily_scaled_kwh": daily_profile(rows, max_gap_seconds),
    }


def collect_profiles(
    db_path: str,
    start: Optional[str],
    end: Optional[str],
    max_gap_seconds: float,
    threshold_w: Optional[float],
) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    profiles: List[Dict[str, Any]] = []

    try:
        for source in SOURCES:
            table = source["table"]
            if not table_exists(conn, table):
                continue

            where = ["power_w IS NOT NULL"]
            params: List[Any] = []
            if start:
                where.append("datetime >= ?")
                params.append(start)
            if end:
                where.append("datetime <= ?")
                params.append(end)

            id_col = source["id_col"]
            name_col = source["name_col"]
            devices = conn.execute(
                f"""
                SELECT {id_col} AS device_id, COALESCE({name_col}, {id_col}) AS label
                FROM {table}
                WHERE {" AND ".join(where)}
                GROUP BY {id_col}, COALESCE({name_col}, {id_col})
                ORDER BY label
                """,
                params,
            ).fetchall()

            for device in devices:
                rows = conn.execute(
                    f"""
                    SELECT timestamp, datetime, power_w, current_a, total_kwh, is_on
                    FROM {table}
                    WHERE {" AND ".join(where)} AND {id_col} = ?
                    ORDER BY timestamp
                    """,
                    [*params, device["device_id"]],
                ).fetchall()
                if len(rows) < 2:
                    continue
                profiles.append(
                    profile_device(
                        rows,
                        source["source"],
                        device["device_id"],
                        device["label"],
                        max_gap_seconds,
                        threshold_w,
                    )
                )
    finally:
        conn.close()

    return profiles


def get_device_rows(
    conn: sqlite3.Connection,
    source_name: str,
    device_id: str,
    start: Optional[str],
    end: Optional[str],
) -> List[sqlite3.Row]:
    source = get_source_config(source_name)
    table = source["table"]
    id_col = source["id_col"]

    if not table_exists(conn, table):
        return []

    where = [f"{id_col} = ?", "power_w IS NOT NULL"]
    params: List[Any] = [device_id]
    if start:
        where.append("datetime >= ?")
        params.append(start)
    if end:
        where.append("datetime <= ?")
        params.append(end)

    return conn.execute(
        f"""
        SELECT timestamp, datetime, power_w, current_a, total_kwh, is_on
        FROM {table}
        WHERE {" AND ".join(where)}
        ORDER BY timestamp
        """,
        params,
    ).fetchall()


def latest_device_label(
    conn: sqlite3.Connection,
    source_name: str,
    device_id: str,
    fallback: Optional[str] = None,
) -> str:
    source = get_source_config(source_name)
    table = source["table"]
    id_col = source["id_col"]
    name_col = source["name_col"]

    if table_exists(conn, table):
        row = conn.execute(
            f"""
            SELECT COALESCE({name_col}, {id_col}) AS label
            FROM {table}
            WHERE {id_col} = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        if row and row["label"]:
            return str(row["label"])

    return fallback or device_id


def first_device_datetime(
    conn: sqlite3.Connection,
    source_name: str,
    device_id: str,
) -> Optional[str]:
    source = get_source_config(source_name)
    table = source["table"]
    id_col = source["id_col"]

    if not table_exists(conn, table):
        return None

    row = conn.execute(
        f"""
        SELECT datetime
        FROM {table}
        WHERE {id_col} = ?
        ORDER BY timestamp
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()
    return str(row["datetime"]) if row else None


def profile_single_device(
    conn: sqlite3.Connection,
    source_name: str,
    device_id: str,
    label: str,
    start: Optional[str],
    end: Optional[str],
    max_gap_seconds: float,
    threshold_w: Optional[float],
) -> Dict[str, Any]:
    rows = get_device_rows(conn, source_name, device_id, start, end)
    if len(rows) < 2:
        raise ValueError(
            f"Not enough readings for {source_name}:{device_id} between {start or 'beginning'} and {end or 'now'}"
        )
    return profile_device(rows, source_name, device_id, label, max_gap_seconds, threshold_w)


def latest_devices(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for source in SOURCES:
        table = source["table"]
        if not table_exists(conn, table):
            continue
        id_col = source["id_col"]
        name_col = source["name_col"]
        rows = conn.execute(
            f"""
            SELECT {id_col} AS device_id,
                   COALESCE({name_col}, {id_col}) AS label,
                   MIN(datetime) AS first_seen,
                   MAX(datetime) AS last_seen
            FROM {table}
            GROUP BY {id_col}, COALESCE({name_col}, {id_col})
            ORDER BY label
            """
        ).fetchall()
        for row in rows:
            devices.append(
                {
                    "source": source["source"],
                    "device_id": str(row["device_id"]),
                    "label": str(row["label"]),
                    "first_seen": str(row["first_seen"]),
                    "last_seen": str(row["last_seen"]),
                }
            )
    return devices


def active_assignment(
    conn: sqlite3.Connection,
    source_name: str,
    device_id: str,
) -> Optional[sqlite3.Row]:
    ensure_tracking_tables(conn)
    return conn.execute(
        f"""
        SELECT *
        FROM {PLUG_ASSIGNMENTS_TABLE}
        WHERE source = ? AND device_id = ? AND assigned_to IS NULL
        ORDER BY assigned_from DESC, id DESC
        LIMIT 1
        """,
        (source_name, device_id),
    ).fetchone()


def seed_active_assignments(db_path: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    seeded: List[Dict[str, Any]] = []

    try:
        ensure_tracking_tables(conn)
        for device in latest_devices(conn):
            existing = active_assignment(conn, device["source"], device["device_id"])
            if existing:
                continue
            assigned_from = device["first_seen"] or now_iso()
            conn.execute(
                f"""
                INSERT INTO {PLUG_ASSIGNMENTS_TABLE}
                    (source, device_id, appliance_label, assigned_from, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    device["source"],
                    device["device_id"],
                    device["label"],
                    assigned_from,
                    "Seeded from current smart-plug label.",
                    now_iso(),
                ),
            )
            seeded.append(device)
        conn.commit()
    finally:
        conn.close()

    return seeded


def create_profile_snapshot(
    conn: sqlite3.Connection,
    source_name: str,
    device_id: str,
    appliance_label: str,
    start: Optional[str],
    end: Optional[str],
    notes: Optional[str],
    max_gap_seconds: float,
    threshold_w: Optional[float],
) -> Dict[str, Any]:
    ensure_tracking_tables(conn)
    profile = profile_single_device(
        conn,
        source_name,
        device_id,
        appliance_label,
        start,
        end,
        max_gap_seconds,
        threshold_w,
    )
    cursor = conn.execute(
        f"""
        INSERT INTO {PROFILE_SNAPSHOTS_TABLE}
            (created_at, source, device_id, appliance_label, profile_start, profile_end, profile_json, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            source_name,
            device_id,
            appliance_label,
            profile.get("start"),
            profile.get("end"),
            json.dumps(profile, ensure_ascii=False),
            notes,
        ),
    )
    conn.commit()
    return {"snapshot_id": int(cursor.lastrowid), "profile": profile}


def before_move(
    db_path: str,
    source_name: str,
    device_id: str,
    at: Optional[str],
    label: Optional[str],
    notes: Optional[str],
    max_gap_seconds: float,
    threshold_w: Optional[float],
) -> Dict[str, Any]:
    effective_at = at or now_iso()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        ensure_tracking_tables(conn)
        assignment = active_assignment(conn, source_name, device_id)
        if assignment:
            appliance_label = label or str(assignment["appliance_label"])
            start = str(assignment["assigned_from"])
            assignment_id = int(assignment["id"])
        else:
            appliance_label = label or latest_device_label(conn, source_name, device_id)
            start = first_device_datetime(conn, source_name, device_id)
            assignment_id = None

        snapshot = create_profile_snapshot(
            conn,
            source_name,
            device_id,
            appliance_label,
            start,
            effective_at,
            notes or "Snapshot taken before moving smart plug.",
            max_gap_seconds,
            threshold_w,
        )

        if assignment_id is not None:
            conn.execute(
                f"""
                UPDATE {PLUG_ASSIGNMENTS_TABLE}
                SET assigned_to = ?, profile_snapshot_id = ?, notes = COALESCE(notes, ?)
                WHERE id = ?
                """,
                (
                    effective_at,
                    snapshot["snapshot_id"],
                    notes or "Closed before moving smart plug.",
                    assignment_id,
                ),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO {PLUG_ASSIGNMENTS_TABLE}
                    (source, device_id, appliance_label, assigned_from, assigned_to, profile_snapshot_id, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
                    device_id,
                    appliance_label,
                    start or snapshot["profile"]["start"],
                    effective_at,
                    snapshot["snapshot_id"],
                    notes or "Historical assignment inferred before moving smart plug.",
                    now_iso(),
                ),
            )
        conn.commit()
        return {
            "action": "before_move",
            "source": source_name,
            "device_id": device_id,
            "appliance_label": appliance_label,
            "effective_at": effective_at,
            **snapshot,
        }
    finally:
        conn.close()


def after_move(
    db_path: str,
    source_name: str,
    device_id: str,
    appliance_label: str,
    at: Optional[str],
    notes: Optional[str],
) -> Dict[str, Any]:
    effective_at = at or now_iso()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        ensure_tracking_tables(conn)
        assignment = active_assignment(conn, source_name, device_id)
        if assignment:
            conn.execute(
                f"""
                UPDATE {PLUG_ASSIGNMENTS_TABLE}
                SET assigned_to = ?, notes = COALESCE(notes, ?)
                WHERE id = ?
                """,
                (
                    effective_at,
                    "Closed automatically before starting a new assignment.",
                    int(assignment["id"]),
                ),
            )
        cursor = conn.execute(
            f"""
            INSERT INTO {PLUG_ASSIGNMENTS_TABLE}
                (source, device_id, appliance_label, assigned_from, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_name,
                device_id,
                appliance_label,
                effective_at,
                notes or "Started after moving smart plug.",
                now_iso(),
            ),
        )
        conn.commit()
        return {
            "action": "after_move",
            "assignment_id": int(cursor.lastrowid),
            "source": source_name,
            "device_id": device_id,
            "appliance_label": appliance_label,
            "effective_at": effective_at,
        }
    finally:
        conn.close()


def list_assignments(db_path: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_tracking_tables(conn)
        rows = conn.execute(
            f"""
            SELECT id, source, device_id, appliance_label, assigned_from, assigned_to,
                   profile_snapshot_id, notes
            FROM {PLUG_ASSIGNMENTS_TABLE}
            ORDER BY source, device_id, assigned_from
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def write_markdown(path: Path, profiles: List[Dict[str, Any]], db_path: str) -> None:
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines = [
        "# Appliance Profiles",
        "",
        f"Generated at `{generated_at}` from `{db_path}`.",
        "",
        "These profiles use integrated `power_w` samples, not plug `total_kwh` counters, so they survive plug counter resets and are safe to keep after moving a plug.",
        "",
        "| Appliance | Window | kWh/day | Avg W | Duty | Run W | Cycle | Coverage |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]

    for profile in profiles:
        run_w = profile["power_w"].get("running_median")
        cycle = "n/a"
        if profile.get("on_duration_min_median") is not None and profile.get("off_duration_min_median") is not None:
            cycle = f"{profile['on_duration_min_median']:.0f}m on / {profile['off_duration_min_median']:.0f}m off"
        lines.append(
            "| {label} | {start} to {end} | {daily:.3f} | {avg:.1f} | {duty:.1f}% | {run} | {cycle} | {coverage:.1f}% |".format(
                label=profile["label"],
                start=profile["start"][:16],
                end=profile["end"][:16],
                daily=profile["scaled_daily_kwh"],
                avg=profile["avg_w"],
                duty=profile["duty_cycle_pct"],
                run=f"{run_w:.1f}W" if run_w is not None else "n/a",
                cycle=cycle,
                coverage=profile["coverage_pct"],
            )
        )

    lines.extend(["", "## Hourly Average Watts", ""])
    for profile in profiles:
        hourly = profile.get("hourly_avg_w") or {}
        lines.append(f"### {profile['label']}")
        lines.append("")
        lines.append(" ".join(f"`{hour}: {watts:.0f}W`" for hour, watts in sorted(hourly.items())))
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(profiles: List[Dict[str, Any]]) -> None:
    for profile in profiles:
        cycle = "n/a"
        if profile.get("on_duration_min_median") is not None and profile.get("off_duration_min_median") is not None:
            cycle = f"{profile['on_duration_min_median']:.1f}m on / {profile['off_duration_min_median']:.1f}m off"
        run_median = profile["power_w"].get("running_median")
        run_text = f"{run_median:.1f} W" if run_median is not None else "n/a"
        print(f"\n== {profile['label']} ({profile['source']}) ==")
        print(f"{profile['start']} -> {profile['end']} rows={profile['rows']} coverage={profile['coverage_pct']:.1f}%")
        print(f"{profile['integrated_kwh']:.3f} kWh measured, {profile['scaled_daily_kwh']:.3f} kWh/day, avg {profile['avg_w']:.1f} W")
        print(f"duty {profile['duty_cycle_pct']:.1f}%, run median {run_text}, cycle {cycle}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="p1_data.db", help="SQLite DB path")
    parser.add_argument("--start", help="ISO datetime lower bound, e.g. 2026-04-24T00:00:00")
    parser.add_argument("--end", help="ISO datetime upper bound")
    parser.add_argument("--max-gap-seconds", type=float, default=180)
    parser.add_argument("--threshold-w", type=float, help="Override inferred run/off threshold")
    parser.add_argument("--json", type=Path, help="Write profiles to JSON")
    parser.add_argument("--markdown", type=Path, help="Write human summary to Markdown")
    parser.add_argument("--init-assignments", action="store_true", help="Seed active plug assignments from current labels")
    parser.add_argument("--list-assignments", action="store_true", help="List tracked plug/appliance assignments")
    parser.add_argument(
        "--before-move",
        nargs=2,
        metavar=("SOURCE", "DEVICE_ID"),
        help="Snapshot and close the current appliance assignment before moving a plug",
    )
    parser.add_argument(
        "--after-move",
        nargs=3,
        metavar=("SOURCE", "DEVICE_ID", "APPLIANCE_LABEL"),
        help="Start a new appliance assignment after moving a plug",
    )
    parser.add_argument("--at", help="Effective ISO datetime for before/after move commands")
    parser.add_argument("--label", help="Override appliance label for --before-move")
    parser.add_argument("--notes", help="Optional notes for assignment/profile records")
    args = parser.parse_args()

    if args.init_assignments:
        seeded = seed_active_assignments(args.db)
        print(f"Seeded {len(seeded)} active assignment(s).")
        for device in seeded:
            print(f"- {device['source']}:{device['device_id']} -> {device['label']}")

    if args.before_move:
        result = before_move(
            args.db,
            args.before_move[0],
            args.before_move[1],
            args.at,
            args.label,
            args.notes,
            args.max_gap_seconds,
            args.threshold_w,
        )
        profile = result["profile"]
        print(
            "Closed {source}:{device_id} as {label}; snapshot #{snapshot_id}, {daily:.3f} kWh/day.".format(
                source=result["source"],
                device_id=result["device_id"],
                label=result["appliance_label"],
                snapshot_id=result["snapshot_id"],
                daily=profile["scaled_daily_kwh"],
            )
        )

    if args.after_move:
        result = after_move(
            args.db,
            args.after_move[0],
            args.after_move[1],
            args.after_move[2],
            args.at,
            args.notes,
        )
        print(
            "Started assignment #{assignment_id}: {source}:{device_id} -> {label} at {at}.".format(
                assignment_id=result["assignment_id"],
                source=result["source"],
                device_id=result["device_id"],
                label=result["appliance_label"],
                at=result["effective_at"],
            )
        )

    if args.list_assignments:
        assignments = list_assignments(args.db)
        if assignments:
            for item in assignments:
                end = item["assigned_to"] or "active"
                snap = f", snapshot #{item['profile_snapshot_id']}" if item["profile_snapshot_id"] else ""
                print(
                    "#{id} {source}:{device_id} -> {label} {start} to {end}{snap}".format(
                        id=item["id"],
                        source=item["source"],
                        device_id=item["device_id"],
                        label=item["appliance_label"],
                        start=item["assigned_from"],
                        end=end,
                        snap=snap,
                    )
                )
        else:
            print("No plug assignments tracked yet.")

    if args.init_assignments or args.before_move or args.after_move or args.list_assignments:
        return

    profiles = collect_profiles(
        args.db,
        args.start,
        args.end,
        args.max_gap_seconds,
        args.threshold_w,
    )

    print_summary(profiles)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": args.db,
        "start": args.start,
        "end": args.end,
        "max_gap_seconds": args.max_gap_seconds,
        "profiles": profiles,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(args.markdown, profiles, args.db)


if __name__ == "__main__":
    main()
