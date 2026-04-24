#!/usr/bin/env python3
"""
HomeWizard P1 Web Monitor with PI Agent Chat Integration
Flask-based web interface for real-time monitoring and AI analysis
"""

import argparse
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import json
import hashlib
import secrets
import shutil
import signal
import threading
import time
import requests
from datetime import datetime, timedelta
import pandas as pd
import os
import sqlite3
from pathlib import Path
from p1_monitor import P1Monitor
import subprocess
import sys
from werkzeug.utils import secure_filename

# Load .env before importing modules that read env vars
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if "=" in _line and not _line.startswith("#"):
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from pi_agent_integration import PiAgentChat
from spot_price import get_fi_spot_price, get_fi_spot_price_series, ensure_spot_price_table
import kasa_monitor
import melcloud_monitor
import tuya_monitor
from chat_agent import PowerAnalysisAgent
from invoice_parser import InvoiceParseError, parse_invoice_pdf

app = Flask(__name__)
app.config['SECRET_KEY'] = (
    os.environ.get('FLASK_SECRET_KEY')
    or os.environ.get('SECRET_KEY')
    or secrets.token_hex(32)
)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
socketio = SocketIO(app, cors_allowed_origins="*")

# Global variables
data_collector = None
kasa_collector = None
tuya_collector = None
melcloud_collector = None
latest_data = {}
latest_kasa_by_mac = {}
latest_tuya_by_uid = {}
latest_melcloud_by_device = {}
monitoring_active = False
collector_supervisor = None
MELCLOUD_POLL_INTERVAL_SECONDS = 120
pi_chat = PiAgentChat()  # legacy fallback (keyword templates, no API key needed)
power_agent = PowerAnalysisAgent()  # agentic Claude-backed analyzer
chat_histories = {}  # session_id -> list[message dict], bounded per conversation
CHAT_HISTORY_MAX_TURNS = 12
DEFAULT_ELECTRICITY_PRICE_EUR_PER_KWH = 0.25
DEFAULT_MONITORING_STALE_SECONDS = 180
COLLECTOR_WATCHDOG_INTERVAL_SECONDS = 30
KASA_POLL_INTERVAL_SECONDS = 60
TUYA_POLL_INTERVAL_SECONDS = 60
KASA_DB_PATH = 'p1_data.db'
MAX_SUMMARY_SAMPLE_GAP_SECONDS = 5 * 60
INVOICE_STORAGE_DIR = Path(__file__).parent / "invoice_pdfs"
LOCALHOST_ADDRS = {"127.0.0.1", "::1"}
MELCLOUD_P1_SCOPE_BY_DEVICE_ID = {
    11309888: {
        'included_in_p1': True,
        'scope_label': 'Included in this P1 meter',
    },
    11432844: {
        'included_in_p1': False,
        'scope_label': 'Outside this P1 meter',
    },
}
MELCLOUD_P1_SCOPE_BY_NAME = {
    'Yläkerta': {
        'included_in_p1': True,
        'scope_label': 'Included in this P1 meter',
    },
    'Iso huone': {
        'included_in_p1': False,
        'scope_label': 'Outside this P1 meter',
    },
}


def get_pricing_context():
    """Resolve current electricity pricing for UI and estimates."""
    spot_price = get_fi_spot_price()
    if spot_price.get('available') and spot_price.get('price_eur_per_kwh') is not None:
        price_eur_per_kwh = float(spot_price['price_eur_per_kwh'])
        using_fallback_price = False
    else:
        price_eur_per_kwh = DEFAULT_ELECTRICITY_PRICE_EUR_PER_KWH
        using_fallback_price = True

    return {
        'spot_price': spot_price,
        'price_eur_per_kwh': price_eur_per_kwh,
        'using_fallback_price': using_fallback_price,
    }


def _collection_should_run(stop_event=None):
    return monitoring_active and not (stop_event and stop_event.is_set())


def _wait_for_shutdown(delay_seconds: float, stop_event=None):
    if stop_event is not None:
        return stop_event.wait(delay_seconds)
    time.sleep(delay_seconds)
    return False


def get_melcloud_scope(device_id: int | None, name: str | None = None):
    """Return whether a MELCloud device is measured by this P1 meter."""
    meta = MELCLOUD_P1_SCOPE_BY_DEVICE_ID.get(device_id)
    if meta:
        return dict(meta)
    if name:
        meta = MELCLOUD_P1_SCOPE_BY_NAME.get(name)
        if meta:
            return dict(meta)
    return {
        'included_in_p1': None,
        'scope_label': 'P1 scope unknown',
    }


def enrich_melcloud_readings(readings, include_estimated_power: bool = False):
    """Attach P1-scope metadata (and optional estimated watts) to MELCloud payloads."""
    enriched = []
    for reading in readings or []:
        item = dict(reading)
        item.update(get_melcloud_scope(item.get('device_id'), item.get('name')))
        if include_estimated_power and item.get('device_id') is not None:
            item['estimated_power_w'] = melcloud_monitor.estimate_current_power_w(
                item['device_id'], KASA_DB_PATH,
            )
        enriched.append(item)
    return enriched


def _sort_plug_rows(rows):
    return sorted(
        rows,
        key=lambda item: (
            (item.get('alias') or item.get('name') or '').lower(),
            item.get('ip') or '',
            item.get('plug_id') or item.get('device_uid') or item.get('mac') or '',
        ),
    )


def get_combined_latest_plugs():
    plugs = []
    for row in kasa_monitor.get_latest_readings(KASA_DB_PATH):
        plugs.append({
            **row,
            'plug_id': f"kasa:{row['mac']}",
            'alias': row.get('alias') or row.get('ip') or row.get('mac'),
            'source': 'kasa',
            'source_label': 'Kasa HS110',
            'needs_setup': False,
            'status_note': '',
        })
    for row in tuya_monitor.get_latest_readings(KASA_DB_PATH):
        plugs.append({
            **row,
            'plug_id': row['device_uid'],
            'alias': row.get('name') or row.get('ip') or row.get('mac') or row['device_uid'],
            'source': 'tuya',
            'source_label': 'Tuya / Nedis',
        })
    return _sort_plug_rows(plugs)


def get_combined_plug_metadata():
    plugs = []
    for row in kasa_monitor.list_plugs(KASA_DB_PATH):
        plugs.append({
            **row,
            'plug_id': f"kasa:{row['mac']}",
            'alias': row.get('alias') or row.get('ip') or row.get('mac'),
            'source': 'kasa',
            'source_label': 'Kasa HS110',
            'needs_setup': False,
        })
    for row in tuya_monitor.list_plugs(KASA_DB_PATH):
        plugs.append({
            **row,
            'plug_id': row['device_uid'],
            'alias': row.get('name') or row.get('ip') or row.get('mac') or row['device_uid'],
            'source': 'tuya',
            'source_label': 'Tuya / Nedis',
            'needs_setup': not row.get('local_key_present'),
        })
    return _sort_plug_rows(plugs)


def get_combined_plug_history(start_ts, end_ts):
    readings = []
    for row in kasa_monitor.get_history(KASA_DB_PATH, start_ts, end_ts, mac=None):
        readings.append({
            **row,
            'plug_id': f"kasa:{row['mac']}",
            'alias': row.get('alias') or row.get('mac'),
            'source': 'kasa',
            'source_label': 'Kasa HS110',
        })
    for row in tuya_monitor.get_history(KASA_DB_PATH, start_ts, end_ts, device_uid=None):
        readings.append({
            **row,
            'plug_id': row['device_uid'],
            'alias': row.get('name') or row.get('device_uid'),
            'source': 'tuya',
            'source_label': 'Tuya / Nedis',
        })
    readings.sort(key=lambda item: item['timestamp'])
    return readings


def resolve_time_window(default_hours: int = 168):
    """Resolve either an explicit start/end range or a trailing hours window."""
    start_param = request.args.get('start')
    end_param = request.args.get('end')
    if start_param or end_param:
        if not start_param or not end_param:
            raise ValueError('start and end parameters required together')
        start_ts = datetime.fromisoformat(start_param.replace('Z', '+00:00')).timestamp()
        end_ts = datetime.fromisoformat(end_param.replace('Z', '+00:00')).timestamp()
        return start_ts, end_ts, {
            'start': start_param,
            'end': end_param,
            'hours': None,
        }

    hours = request.args.get('hours', default_hours, type=int)
    end_ts = time.time()
    start_ts = end_ts - hours * 3600
    return start_ts, end_ts, {
        'start': datetime.fromtimestamp(start_ts).isoformat(),
        'end': datetime.fromtimestamp(end_ts).isoformat(),
        'hours': hours,
    }


def parse_iso_timestamp(value: str) -> float:
    """Parse either UTC ISO strings or local datetime-local strings."""
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def _lerp_value(start_ts: float, start_value: float, end_ts: float, end_value: float, target_ts: float) -> float:
    if end_ts == start_ts:
        return float(start_value)
    ratio = (target_ts - start_ts) / (end_ts - start_ts)
    return float(start_value) + (float(end_value) - float(start_value)) * ratio


def _integrate_positive_linear(start_w: float, end_w: float, duration_hours: float) -> float:
    """Integrate positive power over a linear segment, returning Wh."""
    if duration_hours <= 0:
        return 0.0
    start_w = float(start_w)
    end_w = float(end_w)
    if start_w >= 0 and end_w >= 0:
        return ((start_w + end_w) / 2.0) * duration_hours
    if start_w <= 0 and end_w <= 0:
        return 0.0

    zero_ratio = start_w / (start_w - end_w)
    zero_ratio = max(0.0, min(1.0, zero_ratio))
    if start_w > 0:
        return ((start_w + 0.0) / 2.0) * (duration_hours * zero_ratio)
    return ((0.0 + end_w) / 2.0) * (duration_hours * (1.0 - zero_ratio))


def _fetch_power_samples_for_summary(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH):
    """Fetch in-range samples plus immediate neighbors for boundary interpolation."""
    conn = sqlite3.connect(db_path)
    try:
        prev_row = conn.execute(
            '''
            SELECT timestamp, total_power_w, total_import_kwh, total_export_kwh
            FROM power_data
            WHERE timestamp < ?
            ORDER BY timestamp DESC
            LIMIT 1
            ''',
            (start_ts,),
        ).fetchone()
        in_range_rows = conn.execute(
            '''
            SELECT timestamp, total_power_w, total_import_kwh, total_export_kwh
            FROM power_data
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
            ''',
            (start_ts, end_ts),
        ).fetchall()
        next_row = conn.execute(
            '''
            SELECT timestamp, total_power_w, total_import_kwh, total_export_kwh
            FROM power_data
            WHERE timestamp > ?
            ORDER BY timestamp ASC
            LIMIT 1
            ''',
            (end_ts,),
        ).fetchone()
    finally:
        conn.close()

    merged = {}
    for row in [prev_row, *in_range_rows, next_row]:
        if row is None:
            continue
        merged[float(row[0])] = {
            'timestamp': float(row[0]),
            'power_w': float(row[1] or 0.0),
            'import_kwh': float(row[2]) if row[2] is not None else None,
            'export_kwh': float(row[3]) if row[3] is not None else None,
        }
    samples = [merged[key] for key in sorted(merged)]
    return samples, len(in_range_rows)


def _build_measured_power_segments(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH):
    """Build interpolated measured-power segments within the requested range."""
    samples, sample_count = _fetch_power_samples_for_summary(start_ts, end_ts, db_path=db_path)
    segments = []
    for left, right in zip(samples, samples[1:]):
        seg_start = float(left['timestamp'])
        seg_end = float(right['timestamp'])
        if seg_end <= seg_start or (seg_end - seg_start) > MAX_SUMMARY_SAMPLE_GAP_SECONDS:
            continue

        overlap_start = max(start_ts, seg_start)
        overlap_end = min(end_ts, seg_end)
        if overlap_end <= overlap_start:
            continue

        segments.append({
            'start_ts': overlap_start,
            'end_ts': overlap_end,
            'start_w': _lerp_value(seg_start, left['power_w'], seg_end, right['power_w'], overlap_start),
            'end_w': _lerp_value(seg_start, left['power_w'], seg_end, right['power_w'], overlap_end),
            'source': 'measured',
        })
    return segments, sample_count


def _build_p1_counter_fill_segments(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH):
    """Fill sample gaps with energy deltas from P1 cumulative import/export counters."""
    samples, _ = _fetch_power_samples_for_summary(start_ts, end_ts, db_path=db_path)
    segments = []
    for left, right in zip(samples, samples[1:]):
        seg_start = float(left['timestamp'])
        seg_end = float(right['timestamp'])
        seg_seconds = seg_end - seg_start
        if seg_seconds <= MAX_SUMMARY_SAMPLE_GAP_SECONDS:
            continue

        left_import = left.get('import_kwh')
        right_import = right.get('import_kwh')
        left_export = left.get('export_kwh')
        right_export = right.get('export_kwh')
        if left_import is None or right_import is None:
            continue

        import_delta_kwh = right_import - left_import
        export_delta_kwh = (right_export - left_export) if left_export is not None and right_export is not None else 0.0
        if import_delta_kwh < 0 or export_delta_kwh < 0:
            continue

        overlap_start = max(start_ts, seg_start)
        overlap_end = min(end_ts, seg_end)
        overlap_seconds = overlap_end - overlap_start
        if overlap_seconds <= 0:
            continue

        ratio = overlap_seconds / seg_seconds
        import_wh = import_delta_kwh * 1000.0 * ratio
        export_wh = export_delta_kwh * 1000.0 * ratio
        net_wh = import_wh - export_wh
        avg_power_w = net_wh / (overlap_seconds / 3600.0) if overlap_seconds > 0 else 0.0

        segments.append({
            'start_ts': overlap_start,
            'end_ts': overlap_end,
            'start_w': avg_power_w,
            'end_w': avg_power_w,
            'source': 'p1_counter_fill',
            'import_wh': import_wh,
            'export_wh': export_wh,
            'net_wh': net_wh,
            'full_gap_seconds': seg_seconds,
        })
    return segments


def _segment_energy_wh(segment: dict):
    """Return net/import/export Wh for a segment."""
    if segment.get('import_wh') is not None:
        return (
            float(segment.get('net_wh') or 0.0),
            float(segment.get('import_wh') or 0.0),
            float(segment.get('export_wh') or 0.0),
        )

    duration_hours = (float(segment['end_ts']) - float(segment['start_ts'])) / 3600.0
    start_w = float(segment['start_w'])
    end_w = float(segment['end_w'])
    net_wh = ((start_w + end_w) / 2.0) * duration_hours
    import_wh = _integrate_positive_linear(start_w, end_w, duration_hours)
    export_wh = _integrate_positive_linear(-start_w, -end_w, duration_hours)
    return net_wh, import_wh, export_wh


def _merge_intervals(intervals):
    merged = []
    for start_ts, end_ts in sorted(intervals):
        if end_ts <= start_ts:
            continue
        if not merged or start_ts > merged[-1][1]:
            merged.append([start_ts, end_ts])
        else:
            merged[-1][1] = max(merged[-1][1], end_ts)
    return [(start_ts, end_ts) for start_ts, end_ts in merged]


def _subtract_intervals(start_ts: float, end_ts: float, covered_intervals):
    """Return uncovered intervals inside [start_ts, end_ts)."""
    if end_ts <= start_ts:
        return []

    gaps = []
    cursor = start_ts
    for covered_start, covered_end in _merge_intervals(covered_intervals):
        if covered_end <= cursor:
            continue
        if covered_start > cursor:
            gaps.append((cursor, min(covered_start, end_ts)))
        cursor = max(cursor, covered_end)
        if cursor >= end_ts:
            break
    if cursor < end_ts:
        gaps.append((cursor, end_ts))
    return [(gap_start, gap_end) for gap_start, gap_end in gaps if gap_end > gap_start]


def _fetch_overlapping_invoices(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH):
    ensure_invoice_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''
            SELECT *
            FROM electricity_invoices
            WHERE period_end_ts > ? AND period_start_ts < ?
            ORDER BY period_start_ts ASC
            ''',
            (start_ts, end_ts),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _invoice_average_price_eur_per_kwh(invoice: dict) -> float | None:
    avg_cents = invoice.get('average_price_cents_per_kwh')
    if avg_cents is not None:
        try:
            avg_cents = float(avg_cents)
        except (TypeError, ValueError):
            avg_cents = None
        if avg_cents is not None and avg_cents > 0:
            return avg_cents / 100.0

    billed_kwh = float(invoice.get('billed_energy_kwh') or 0.0)
    total_amount = invoice.get('total_amount_eur')
    if billed_kwh > 0 and total_amount is not None:
        try:
            return float(total_amount) / billed_kwh
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _compute_time_weighted_spot_average(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH):
    duration_seconds = max(0.0, end_ts - start_ts)
    if duration_seconds <= 0:
        return None, 0.0

    spot_prices = get_fi_spot_price_series(start_ts, end_ts, db_path=db_path)
    covered_seconds = 0.0
    weighted_sum = 0.0
    for price in spot_prices:
        overlap_start = max(start_ts, float(price['start_ts']))
        overlap_end = min(end_ts, float(price['end_ts']))
        if overlap_end <= overlap_start:
            continue
        overlap_seconds = overlap_end - overlap_start
        weighted_sum += float(price['price_eur_per_kwh']) * overlap_seconds
        covered_seconds += overlap_seconds

    if covered_seconds <= 0:
        return None, 0.0
    return weighted_sum / covered_seconds, (covered_seconds / duration_seconds) * 100.0


def _build_invoice_profile(invoice: dict, db_path: str = KASA_DB_PATH):
    invoice = dict(invoice)
    invoice_start = float(invoice.get('period_start_ts') or 0.0)
    invoice_end = float(invoice.get('period_end_ts') or 0.0)
    billed_kwh = float(invoice.get('billed_energy_kwh') or 0.0)
    invoice_duration_seconds = max(0.0, invoice_end - invoice_start)

    measured_segments, _ = _build_measured_power_segments(invoice_start, invoice_end, db_path=db_path)
    counter_fill_segments = _build_p1_counter_fill_segments(invoice_start, invoice_end, db_path=db_path)
    p1_actual_segments = [*measured_segments, *counter_fill_segments]
    measured_intervals = _merge_intervals(
        [(segment['start_ts'], segment['end_ts']) for segment in p1_actual_segments]
    )

    measured_import_wh = 0.0
    measured_seconds = 0.0
    counter_fill_seconds = 0.0
    counter_fill_import_wh = 0.0
    for segment in p1_actual_segments:
        segment_seconds = float(segment['end_ts']) - float(segment['start_ts'])
        if segment_seconds <= 0:
            continue
        measured_seconds += segment_seconds
        _net_wh, import_wh, _export_wh = _segment_energy_wh(segment)
        measured_import_wh += import_wh
        if segment.get('source') == 'p1_counter_fill':
            counter_fill_seconds += segment_seconds
            counter_fill_import_wh += import_wh
    uncovered_seconds = max(0.0, invoice_duration_seconds - measured_seconds)
    remaining_wh = max(0.0, billed_kwh * 1000.0 - measured_import_wh)
    remaining_avg_power_w = (remaining_wh / (uncovered_seconds / 3600.0)) if uncovered_seconds > 0 else None

    invoice_avg_price_eur_per_kwh = _invoice_average_price_eur_per_kwh(invoice)
    spot_avg_price_eur_per_kwh, spot_coverage_pct = _compute_time_weighted_spot_average(
        invoice_start,
        invoice_end,
        db_path=db_path,
    ) if invoice_duration_seconds > 0 else (None, 0.0)

    price_delta_eur_per_kwh = None
    if (
        invoice_avg_price_eur_per_kwh is not None
        and spot_avg_price_eur_per_kwh is not None
        and spot_coverage_pct >= 95.0
    ):
        price_delta_eur_per_kwh = invoice_avg_price_eur_per_kwh - spot_avg_price_eur_per_kwh

    invoice['invoice_avg_price_eur_per_kwh'] = invoice_avg_price_eur_per_kwh
    invoice['spot_avg_price_eur_per_kwh'] = spot_avg_price_eur_per_kwh
    invoice['spot_coverage_pct'] = round(spot_coverage_pct, 1)
    invoice['price_delta_eur_per_kwh'] = price_delta_eur_per_kwh
    invoice['remaining_avg_power_w'] = remaining_avg_power_w
    invoice['measured_seconds'] = measured_seconds
    invoice['p1_counter_fill_seconds'] = counter_fill_seconds
    invoice['p1_counter_fill_kwh'] = counter_fill_import_wh / 1000.0
    invoice['uncovered_seconds'] = uncovered_seconds
    invoice['measured_intervals'] = measured_intervals
    return invoice


def _build_invoice_estimate_segments(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH, invoices=None):
    invoices = invoices if invoices is not None else _fetch_overlapping_invoices(start_ts, end_ts, db_path=db_path)
    profiles = {}
    segments = []
    segment_counter = 0

    for invoice in invoices:
        profile = _build_invoice_profile(invoice, db_path=db_path)
        profiles[profile['id']] = profile
        remaining_avg_power_w = profile.get('remaining_avg_power_w')
        if remaining_avg_power_w is None or remaining_avg_power_w <= 0:
            continue

        invoice_start = max(start_ts, float(profile.get('period_start_ts') or 0.0))
        invoice_end = min(end_ts, float(profile.get('period_end_ts') or 0.0))
        if invoice_end <= invoice_start:
            continue

        covered_inside_window = []
        for covered_start, covered_end in profile.get('measured_intervals') or []:
            overlap_start = max(invoice_start, covered_start)
            overlap_end = min(invoice_end, covered_end)
            if overlap_end > overlap_start:
                covered_inside_window.append((overlap_start, overlap_end))

        for gap_start, gap_end in _subtract_intervals(invoice_start, invoice_end, covered_inside_window):
            segment_counter += 1
            segments.append({
                'start_ts': gap_start,
                'end_ts': gap_end,
                'start_w': remaining_avg_power_w,
                'end_w': remaining_avg_power_w,
                'source': 'invoice_estimate',
                'invoice_id': profile.get('id'),
                'invoice_number': profile.get('invoice_number'),
                'seller_name': profile.get('seller_name'),
                'estimate_segment_id': f'invoice-{profile.get("id")}-{segment_counter}',
            })

    return segments, profiles


def _compute_default_invoice_price_delta(db_path: str = KASA_DB_PATH):
    ensure_invoice_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''
            SELECT *
            FROM electricity_invoices
            WHERE billed_energy_kwh IS NOT NULL AND period_end_ts > period_start_ts
            ORDER BY period_start_ts ASC
            '''
        ).fetchall()
    finally:
        conn.close()

    total_weight = 0.0
    weighted_delta = 0.0
    invoice_count = 0
    for row in rows:
        profile = _build_invoice_profile(dict(row), db_path=db_path)
        delta = profile.get('price_delta_eur_per_kwh')
        if delta is None:
            continue
        weight = max(float(profile.get('billed_energy_kwh') or 0.0), 1.0)
        weighted_delta += float(delta) * weight
        total_weight += weight
        invoice_count += 1

    if total_weight <= 0:
        return None, 0
    return weighted_delta / total_weight, invoice_count


def _build_price_windows(start_ts: float, end_ts: float, invoice_profiles: dict[int, dict] | None = None, db_path: str = KASA_DB_PATH):
    pricing = get_pricing_context()
    fallback_price_eur_per_kwh = float(pricing['price_eur_per_kwh'])
    spot_prices = get_fi_spot_price_series(start_ts, end_ts, db_path=db_path)
    direct_invoice_profiles = [
        profile for profile in (invoice_profiles or {}).values()
        if profile.get('invoice_avg_price_eur_per_kwh') is not None
    ]
    default_delta_eur_per_kwh, delta_invoice_count = _compute_default_invoice_price_delta(db_path=db_path)

    change_points = {float(start_ts), float(end_ts)}
    for price in spot_prices:
        change_points.add(max(float(start_ts), float(price['start_ts'])))
        change_points.add(min(float(end_ts), float(price['end_ts'])))
    for profile in direct_invoice_profiles:
        change_points.add(max(float(start_ts), float(profile['period_start_ts'])))
        change_points.add(min(float(end_ts), float(profile['period_end_ts'])))

    points = sorted(point for point in change_points if start_ts <= point <= end_ts)
    if len(points) < 2:
        points = [start_ts, end_ts]

    windows = []
    for window_start, window_end in zip(points, points[1:]):
        if window_end <= window_start:
            continue
        midpoint = window_start + (window_end - window_start) / 2.0
        direct_invoice = next(
            (
                profile for profile in direct_invoice_profiles
                if float(profile['period_start_ts']) <= midpoint < float(profile['period_end_ts'])
            ),
            None,
        )
        spot_price = next(
            (
                price for price in spot_prices
                if float(price['start_ts']) <= midpoint < float(price['end_ts'])
            ),
            None,
        )

        if direct_invoice is not None:
            actual_price = float(direct_invoice['invoice_avg_price_eur_per_kwh'])
            basis = 'invoice_avg'
        else:
            base_price = float(spot_price['price_eur_per_kwh']) if spot_price is not None else fallback_price_eur_per_kwh
            actual_price = base_price + (default_delta_eur_per_kwh or 0.0)
            if spot_price is not None and default_delta_eur_per_kwh is not None:
                basis = 'spot_plus_invoice_delta'
            elif spot_price is not None:
                basis = 'spot'
            elif default_delta_eur_per_kwh is not None:
                basis = 'fallback_plus_invoice_delta'
            else:
                basis = 'fallback'

        windows.append({
            'start_ts': window_start,
            'end_ts': window_end,
            'actual_price_eur_per_kwh': max(0.0, float(actual_price)),
            'spot_price_eur_per_kwh': float(spot_price['price_eur_per_kwh']) if spot_price is not None else None,
            'spot_available': spot_price is not None,
            'direct_invoice_price': direct_invoice is not None,
            'used_invoice_delta': direct_invoice is None and default_delta_eur_per_kwh is not None,
            'invoice_id': direct_invoice.get('id') if direct_invoice is not None else None,
            'basis': basis,
        })

    return {
        'windows': windows,
        'spot_prices': spot_prices,
        'fallback_price_eur_per_kwh': fallback_price_eur_per_kwh,
        'default_delta_eur_per_kwh': default_delta_eur_per_kwh,
        'delta_invoice_count': delta_invoice_count,
        'pricing_context': pricing,
    }


def summarize_power_range(
    start_ts: float,
    end_ts: float,
    db_path: str = KASA_DB_PATH,
    include_invoice_estimates: bool = True,
):
    """Integrate power, invoices, and calibrated pricing over a selected range."""
    duration_seconds = max(0.0, end_ts - start_ts)
    measured_segments, sample_count = _build_measured_power_segments(start_ts, end_ts, db_path=db_path)
    p1_counter_fill_segments = _build_p1_counter_fill_segments(start_ts, end_ts, db_path=db_path)
    overlapping_invoices = _fetch_overlapping_invoices(start_ts, end_ts, db_path=db_path)
    invoice_segments = []
    invoice_profiles = {}
    if include_invoice_estimates:
        invoice_segments, invoice_profiles = _build_invoice_estimate_segments(
            start_ts,
            end_ts,
            db_path=db_path,
            invoices=overlapping_invoices,
        )
    else:
        invoice_profiles = {
            invoice['id']: _build_invoice_profile(invoice, db_path=db_path)
            for invoice in overlapping_invoices
        }

    price_context = _build_price_windows(start_ts, end_ts, invoice_profiles=invoice_profiles, db_path=db_path)
    spot_prices = price_context['spot_prices']
    fallback_price_eur_per_kwh = float(price_context['fallback_price_eur_per_kwh'])
    pricing = price_context['pricing_context']

    summary = {
        'sample_count': sample_count,
        'duration_seconds': round(duration_seconds, 3),
        'duration_hours': round(duration_seconds / 3600.0, 4) if duration_seconds else 0.0,
        'avg_power_w': 0.0,
        'min_power_w': 0.0,
        'max_power_w': 0.0,
        'net_energy_kwh': 0.0,
        'import_energy_kwh': 0.0,
        'export_energy_kwh': 0.0,
        'estimated_cost_eur': 0.0,
        'spot_cost_eur': 0.0,
        'fallback_cost_eur': 0.0,
        'invoice_priced_cost_eur': 0.0,
        'invoice_delta_cost_eur': 0.0,
        'spot_price_coverage_pct': 0.0,
        'data_coverage_pct': 0.0,
        'measured_coverage_pct': 0.0,
        'p1_counter_fill_coverage_pct': 0.0,
        'p1_counter_fill_kwh': 0.0,
        'invoice_estimate_coverage_pct': 0.0,
        'invoice_estimate_kwh': 0.0,
        'invoice_price_coverage_pct': 0.0,
        'invoice_delta_coverage_pct': 0.0,
        'fallback_price_eur_per_kwh': round(fallback_price_eur_per_kwh, 6),
        'fallback_price_cents_per_kwh': round(fallback_price_eur_per_kwh * 100.0, 3),
        'price_delta_cents_per_kwh': round(float(price_context['default_delta_eur_per_kwh']) * 100.0, 3)
            if price_context['default_delta_eur_per_kwh'] is not None else None,
        'price_delta_invoice_count': int(price_context['delta_invoice_count']),
        'spot_price_points': len(spot_prices),
        'using_fallback_price': pricing['using_fallback_price'],
        'spot_price': pricing['spot_price'],
        'covered_seconds': 0.0,
    }

    segments = sorted(
        [*measured_segments, *p1_counter_fill_segments, *invoice_segments],
        key=lambda item: (float(item['start_ts']), 0 if item['source'] == 'measured' else 1),
    )
    if duration_seconds <= 0 or not segments:
        return summary

    net_energy_wh = 0.0
    import_energy_wh = 0.0
    export_energy_wh = 0.0
    invoice_estimate_import_wh = 0.0
    spot_priced_import_wh = 0.0
    invoice_priced_import_wh = 0.0
    delta_priced_import_wh = 0.0
    total_cost_eur = 0.0
    spot_cost_eur = 0.0
    fallback_cost_eur = 0.0
    invoice_priced_cost_eur = 0.0
    invoice_delta_cost_eur = 0.0
    covered_seconds = 0.0
    measured_seconds = 0.0
    p1_counter_fill_seconds = 0.0
    p1_counter_fill_import_wh = 0.0
    invoice_estimate_seconds = 0.0
    observed_values = []

    for segment in segments:
        seg_start = float(segment['start_ts'])
        seg_end = float(segment['end_ts'])
        if seg_end <= seg_start:
            continue

        seg_net_wh, seg_import_wh, seg_export_wh = _segment_energy_wh(segment)
        observed_values.extend([float(segment['start_w']), float(segment['end_w'])])
        covered_seconds += seg_end - seg_start
        if segment['source'] == 'measured':
            measured_seconds += seg_end - seg_start
        elif segment['source'] == 'p1_counter_fill':
            p1_counter_fill_seconds += seg_end - seg_start
            p1_counter_fill_import_wh += seg_import_wh
        elif segment['source'] == 'invoice_estimate':
            invoice_estimate_seconds += seg_end - seg_start
            invoice_estimate_import_wh += seg_import_wh

        net_energy_wh += seg_net_wh
        import_energy_wh += seg_import_wh
        export_energy_wh += seg_export_wh

        for price_window in price_context['windows']:
            overlap_start = max(seg_start, float(price_window['start_ts']))
            overlap_end = min(seg_end, float(price_window['end_ts']))
            if overlap_end <= overlap_start:
                continue

            if segment.get('import_wh') is not None:
                priced_import_wh = seg_import_wh * ((overlap_end - overlap_start) / (seg_end - seg_start))
            else:
                priced_start_w = _lerp_value(seg_start, segment['start_w'], seg_end, segment['end_w'], overlap_start)
                priced_end_w = _lerp_value(seg_start, segment['start_w'], seg_end, segment['end_w'], overlap_end)
                priced_hours = (overlap_end - overlap_start) / 3600.0
                priced_import_wh = _integrate_positive_linear(priced_start_w, priced_end_w, priced_hours)
            priced_cost = (priced_import_wh / 1000.0) * float(price_window['actual_price_eur_per_kwh'])
            total_cost_eur += priced_cost

            if price_window['direct_invoice_price']:
                invoice_priced_import_wh += priced_import_wh
                invoice_priced_cost_eur += priced_cost
            elif price_window['used_invoice_delta']:
                delta_priced_import_wh += priced_import_wh
                invoice_delta_cost_eur += priced_cost
                if price_window['spot_available']:
                    spot_priced_import_wh += priced_import_wh
                    spot_cost_eur += priced_cost
                else:
                    fallback_cost_eur += priced_cost
            elif price_window['spot_available']:
                spot_priced_import_wh += priced_import_wh
                spot_cost_eur += priced_cost
            else:
                fallback_cost_eur += priced_cost

    if covered_seconds > 0:
        covered_hours = covered_seconds / 3600.0
        summary['avg_power_w'] = round(net_energy_wh / covered_hours, 1)
        if observed_values:
            summary['min_power_w'] = round(min(observed_values), 1)
            summary['max_power_w'] = round(max(observed_values), 1)

    summary['net_energy_kwh'] = round(net_energy_wh / 1000.0, 4)
    summary['import_energy_kwh'] = round(import_energy_wh / 1000.0, 4)
    summary['export_energy_kwh'] = round(export_energy_wh / 1000.0, 4)
    summary['estimated_cost_eur'] = round(total_cost_eur, 4)
    summary['spot_cost_eur'] = round(spot_cost_eur, 4)
    summary['fallback_cost_eur'] = round(fallback_cost_eur, 4)
    summary['invoice_priced_cost_eur'] = round(invoice_priced_cost_eur, 4)
    summary['invoice_delta_cost_eur'] = round(invoice_delta_cost_eur, 4)
    summary['covered_seconds'] = round(covered_seconds, 3)
    summary['data_coverage_pct'] = round((covered_seconds / duration_seconds) * 100.0, 1) if duration_seconds else 0.0
    summary['measured_coverage_pct'] = round((measured_seconds / duration_seconds) * 100.0, 1) if duration_seconds else 0.0
    summary['p1_counter_fill_coverage_pct'] = round((p1_counter_fill_seconds / duration_seconds) * 100.0, 1) if duration_seconds else 0.0
    summary['p1_counter_fill_kwh'] = round(p1_counter_fill_import_wh / 1000.0, 4)
    summary['invoice_estimate_coverage_pct'] = round((invoice_estimate_seconds / duration_seconds) * 100.0, 1) if duration_seconds else 0.0
    summary['invoice_estimate_kwh'] = round(invoice_estimate_import_wh / 1000.0, 4)
    if import_energy_wh > 0:
        summary['spot_price_coverage_pct'] = round((spot_priced_import_wh / import_energy_wh) * 100.0, 1)
        summary['invoice_price_coverage_pct'] = round((invoice_priced_import_wh / import_energy_wh) * 100.0, 1)
        summary['invoice_delta_coverage_pct'] = round((delta_priced_import_wh / import_energy_wh) * 100.0, 1)
    else:
        summary['spot_price_coverage_pct'] = 100.0 if duration_seconds and spot_prices else 0.0

    return summary


def ensure_invoice_tables(db_path: str = KASA_DB_PATH) -> None:
    INVOICE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS electricity_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parser_name TEXT,
                seller_name TEXT,
                invoice_number TEXT,
                invoice_date TEXT,
                customer_number TEXT,
                contract_number TEXT,
                usage_point_id TEXT,
                period_start_date TEXT,
                period_end_date TEXT,
                period_start_ts REAL,
                period_end_ts REAL,
                billed_energy_kwh REAL,
                total_amount_eur REAL,
                average_price_cents_per_kwh REAL,
                annual_usage_estimate_kwh REAL,
                service_address TEXT,
                billing_topic TEXT,
                source_filename TEXT,
                stored_pdf_path TEXT,
                pdf_sha256 TEXT UNIQUE,
                imported_at REAL
            )
            '''
        )
        conn.commit()
    finally:
        conn.close()


def is_local_request() -> bool:
    return (request.remote_addr or "") in LOCALHOST_ADDRS


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _get_invoice_by_sha(pdf_sha256: str, db_path: str = KASA_DB_PATH):
    ensure_invoice_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM electricity_invoices WHERE pdf_sha256 = ?",
            (pdf_sha256,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _serialize_invoice_row(row: sqlite3.Row | dict) -> dict:
    return dict(row)


def _correlate_invoice(invoice: dict) -> dict:
    period_start_ts = float(invoice.get("period_start_ts") or 0.0)
    period_end_ts = float(invoice.get("period_end_ts") or 0.0)
    correlation = summarize_power_range(
        period_start_ts,
        period_end_ts,
        KASA_DB_PATH,
        include_invoice_estimates=False,
    ) if period_end_ts > period_start_ts else {}

    billed_energy_kwh = float(invoice.get("billed_energy_kwh") or 0.0)
    duration_hours = (period_end_ts - period_start_ts) / 3600.0 if period_end_ts > period_start_ts else 0.0
    invoice_avg_power_w = (billed_energy_kwh * 1000.0 / duration_hours) if duration_hours > 0 and billed_energy_kwh > 0 else None
    invoice_daily_kwh = (billed_energy_kwh * 24.0 / duration_hours) if duration_hours > 0 and billed_energy_kwh > 0 else None

    measured_import_kwh = float(correlation.get("import_energy_kwh") or 0.0)
    measured_coverage_pct = float(correlation.get("data_coverage_pct") or 0.0)
    projected_full_period_kwh = None
    billed_minus_projected_kwh = None
    billed_minus_projected_pct = None
    if billed_energy_kwh > 0 and measured_coverage_pct > 0:
        projected_full_period_kwh = measured_import_kwh / (measured_coverage_pct / 100.0)
        billed_minus_projected_kwh = billed_energy_kwh - projected_full_period_kwh
        billed_minus_projected_pct = (billed_minus_projected_kwh / billed_energy_kwh) * 100.0

    invoice["correlation"] = {
        "invoice_avg_power_w": round(invoice_avg_power_w, 1) if invoice_avg_power_w is not None else None,
        "invoice_daily_kwh": round(invoice_daily_kwh, 2) if invoice_daily_kwh is not None else None,
        "measured_import_kwh": measured_import_kwh,
        "measured_coverage_pct": float(correlation.get("measured_coverage_pct") or measured_coverage_pct),
        "projected_full_period_kwh": round(projected_full_period_kwh, 2) if projected_full_period_kwh is not None else None,
        "billed_minus_projected_kwh": round(billed_minus_projected_kwh, 2) if billed_minus_projected_kwh is not None else None,
        "billed_minus_projected_pct": round(billed_minus_projected_pct, 1) if billed_minus_projected_pct is not None else None,
        "invoice_price_coverage_pct": float(correlation.get("invoice_price_coverage_pct") or 0.0),
        "invoice_delta_coverage_pct": float(correlation.get("invoice_delta_coverage_pct") or 0.0),
        "price_delta_cents_per_kwh": correlation.get("price_delta_cents_per_kwh"),
        "price_delta_invoice_count": correlation.get("price_delta_invoice_count"),
        "cost_summary": correlation,
    }
    return invoice


def _store_parsed_invoice(parsed: dict, source_filename: str, stored_pdf_path: str, pdf_sha256: str, db_path: str = KASA_DB_PATH):
    ensure_invoice_tables(db_path)
    existing = _get_invoice_by_sha(pdf_sha256, db_path)

    conn = sqlite3.connect(db_path)
    try:
        values = (
            parsed.get("parser_name"),
            parsed.get("seller_name"),
            parsed.get("invoice_number"),
            parsed.get("invoice_date"),
            parsed.get("customer_number"),
            parsed.get("contract_number"),
            parsed.get("usage_point_id"),
            parsed.get("period_start_date"),
            parsed.get("period_end_date"),
            parsed.get("period_start_ts"),
            parsed.get("period_end_ts"),
            parsed.get("billed_energy_kwh"),
            parsed.get("total_amount_eur"),
            parsed.get("average_price_cents_per_kwh"),
            parsed.get("annual_usage_estimate_kwh"),
            parsed.get("service_address"),
            parsed.get("billing_topic"),
            source_filename,
            stored_pdf_path,
            pdf_sha256,
        )

        if existing:
            conn.execute(
                '''
                UPDATE electricity_invoices
                SET parser_name = ?, seller_name = ?, invoice_number = ?, invoice_date = ?,
                    customer_number = ?, contract_number = ?, usage_point_id = ?,
                    period_start_date = ?, period_end_date = ?, period_start_ts = ?, period_end_ts = ?,
                    billed_energy_kwh = ?, total_amount_eur = ?, average_price_cents_per_kwh = ?,
                    annual_usage_estimate_kwh = ?, service_address = ?, billing_topic = ?,
                    source_filename = ?, stored_pdf_path = ?, pdf_sha256 = ?
                WHERE id = ?
                ''',
                values + (existing["id"],),
            )
            invoice_id = existing["id"]
        else:
            conn.execute(
                '''
                INSERT INTO electricity_invoices (
                    parser_name, seller_name, invoice_number, invoice_date,
                    customer_number, contract_number, usage_point_id,
                    period_start_date, period_end_date, period_start_ts, period_end_ts,
                    billed_energy_kwh, total_amount_eur, average_price_cents_per_kwh,
                    annual_usage_estimate_kwh, service_address, billing_topic,
                    source_filename, stored_pdf_path, pdf_sha256, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                values + (time.time(),),
            )
            invoice_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM electricity_invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _correlate_invoice(dict(row)) if row else None
    finally:
        conn.close()


def list_invoices(db_path: str = KASA_DB_PATH):
    ensure_invoice_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''
            SELECT *
            FROM electricity_invoices
            ORDER BY period_start_ts DESC, imported_at DESC
            '''
        ).fetchall()
    finally:
        conn.close()

    invoices = []
    for row in rows:
        invoice = dict(row)
        if invoice.get("stored_pdf_path"):
            invoice["pdf_url"] = f"/api/invoices/{invoice['id']}/pdf"
        invoices.append(_correlate_invoice(invoice))
    return invoices


def _row_to_history_point(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    item['source'] = 'measured'
    item['estimated'] = False
    item['estimate_segment_id'] = None
    return item


def _build_invoice_history_points(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH):
    invoice_segments, _ = _build_invoice_estimate_segments(start_ts, end_ts, db_path=db_path)
    points = []
    for segment in invoice_segments:
        power_w = round(float(segment['start_w']), 1)
        for point_ts in (float(segment['start_ts']), float(segment['end_ts'])):
            points.append({
                'id': None,
                'timestamp': point_ts,
                'datetime': datetime.fromtimestamp(point_ts).isoformat(),
                'total_power_w': power_w,
                'total_import_kwh': None,
                'total_export_kwh': None,
                'power_l1_w': None,
                'power_l2_w': None,
                'power_l3_w': None,
                'voltage_l1_v': None,
                'voltage_l2_v': None,
                'voltage_l3_v': None,
                'current_l1_a': None,
                'current_l2_a': None,
                'current_l3_a': None,
                'current_total_a': None,
                'wifi_strength': None,
                'source': 'invoice_estimate',
                'estimated': True,
                'estimate_segment_id': segment.get('estimate_segment_id'),
                'invoice_id': segment.get('invoice_id'),
                'invoice_number': segment.get('invoice_number'),
                'seller_name': segment.get('seller_name'),
            })
    return points


def _build_history_payload(start_ts: float, end_ts: float, db_path: str = KASA_DB_PATH, measured_limit: int | None = None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = '''
            SELECT *
            FROM power_data
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        '''
        params = [start_ts, end_ts]
        if measured_limit is not None:
            query += ' LIMIT ?'
            params.append(int(measured_limit))
        measured_rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    points = [_row_to_history_point(row) for row in measured_rows]
    points.extend(_build_invoice_history_points(start_ts, end_ts, db_path=db_path))
    points.sort(key=lambda item: (float(item['timestamp']), 0 if item.get('source') == 'measured' else 1))
    return points

class WebP1Monitor(P1Monitor):
    def __init__(self):
        super().__init__(data_file="p1_web_data.csv")
        self.setup_database()
    
    def setup_database(self):
        """Initialize SQLite database for faster queries"""
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS power_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                datetime TEXT,
                total_power_w INTEGER,
                total_import_kwh REAL,
                total_export_kwh REAL,
                power_l1_w INTEGER,
                power_l2_w INTEGER, 
                power_l3_w INTEGER,
                voltage_l1_v REAL,
                voltage_l2_v REAL,
                voltage_l3_v REAL,
                current_l1_a REAL,
                current_l2_a REAL,
                current_l3_a REAL,
                current_total_a REAL,
                wifi_strength INTEGER
            )
        ''')
        
        # Create markers table for chart annotations
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chart_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                datetime TEXT,
                label TEXT,
                description TEXT,
                color TEXT DEFAULT '#ff6b6b',
                created_at REAL
            )
        ''')

        conn.commit()
        conn.close()

        # Cached spot price series (for historical chart overlay)
        ensure_spot_price_table('p1_data.db')

        # Kasa HS110 plug readings (per-appliance power breakdown)
        kasa_monitor.ensure_kasa_tables(KASA_DB_PATH)

        # Tuya / Nedis smart plugs (tinytuya-backed local polling)
        tuya_monitor.ensure_tuya_tables(KASA_DB_PATH)

        # Mitsubishi MELCloud ILP state + lifetime energy
        melcloud_monitor.ensure_melcloud_tables(KASA_DB_PATH)

        # Imported electricity invoice metadata + stored PDFs
        ensure_invoice_tables(KASA_DB_PATH)
    
    def store_data_db(self, data):
        """Store data to SQLite database"""
        if not data:
            return
            
        timestamp = time.time()
        dt = datetime.now()
        
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO power_data (
                timestamp, datetime, total_power_w, total_import_kwh, total_export_kwh,
                power_l1_w, power_l2_w, power_l3_w,
                voltage_l1_v, voltage_l2_v, voltage_l3_v,
                current_l1_a, current_l2_a, current_l3_a, current_total_a,
                wifi_strength
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            timestamp, dt.isoformat(),
            data.get('active_power_w', 0),
            data.get('total_power_import_kwh', 0),
            data.get('total_power_export_kwh', 0),
            data.get('active_power_l1_w', 0),
            data.get('active_power_l2_w', 0), 
            data.get('active_power_l3_w', 0),
            data.get('active_voltage_l1_v', 0),
            data.get('active_voltage_l2_v', 0),
            data.get('active_voltage_l3_v', 0),
            data.get('active_current_l1_a', 0),
            data.get('active_current_l2_a', 0),
            data.get('active_current_l3_a', 0),
            data.get('active_current_a', 0),
            data.get('wifi_strength', 0)
        ))
        conn.commit()
        conn.close()

def background_data_collection(stop_event=None):
    """Background thread for continuous P1 data collection."""
    global latest_data

    monitor = None

    while _collection_should_run(stop_event):
        try:
            if monitor is None:
                monitor = WebP1Monitor()

            data = monitor.fetch_data()
            if data:
                monitor.store_data(data)
                monitor.store_data_db(data)

                pricing = get_pricing_context()
                latest_data = dict(data)
                latest_data['timestamp'] = datetime.now().isoformat()
                latest_data['spot_price'] = pricing['spot_price']
                latest_data['price_eur_per_kwh'] = pricing['price_eur_per_kwh']
                latest_data['using_fallback_price'] = pricing['using_fallback_price']

                socketio.emit('new_data', latest_data)
                print(f"✅ Data collected: {data.get('active_power_w', 0)}W")
            else:
                print("❌ Failed to fetch data")
        except Exception as e:
            monitor = None
            print(f"❌ Error in data collection: {e}")

        if _wait_for_shutdown(60, stop_event):
            break


def background_kasa_collection(stop_event=None):
    """Poll all enabled Kasa plugs, persist readings, broadcast via socket."""
    global latest_kasa_by_mac

    while _collection_should_run(stop_event):
        try:
            readings = kasa_monitor.poll_plugs(KASA_DB_PATH)
            if readings:
                latest_kasa_by_mac = {r['mac']: r for r in readings}
                socketio.emit('kasa_data', {'readings': get_combined_latest_plugs()})
                total = sum(r['power_w'] for r in readings)
                print(f"🔌 Kasa poll: {len(readings)} plug(s), total {total:.1f}W")
        except Exception as e:
            print(f"❌ Kasa poll error: {e}")

        if _wait_for_shutdown(KASA_POLL_INTERVAL_SECONDS, stop_event):
            break


def background_tuya_collection(stop_event=None):
    """Poll all configured Tuya/Nedis plugs, persist readings, broadcast via socket."""
    global latest_tuya_by_uid

    while _collection_should_run(stop_event):
        try:
            readings = tuya_monitor.poll_plugs(KASA_DB_PATH)
            if readings:
                latest_tuya_by_uid = {r['device_uid']: r for r in readings}
                socketio.emit('kasa_data', {'readings': get_combined_latest_plugs()})
                total = sum((r.get('power_w') or 0.0) for r in readings)
                print(f"🔌 Tuya poll: {len(readings)} plug(s), total {total:.1f}W")
        except Exception as e:
            print(f"❌ Tuya poll error: {e}")

        if _wait_for_shutdown(TUYA_POLL_INTERVAL_SECONDS, stop_event):
            break


def background_melcloud_collection(stop_event=None):
    """Poll MELCloud every ~2 min; refresh hourly energy reports every ~1h."""
    global latest_melcloud_by_device

    consecutive_failures = 0
    last_energy_refresh_ts = 0.0
    energy_refresh_interval = 3600

    while _collection_should_run(stop_event):
        try:
            readings = melcloud_monitor.poll_melcloud(KASA_DB_PATH)
            if readings:
                enriched = enrich_melcloud_readings(readings, include_estimated_power=True)
                latest_melcloud_by_device = {r['device_id']: r for r in enriched}
                socketio.emit('melcloud_data', {'readings': enriched})
                for r in enriched:
                    print(
                        f"🔥 MELCloud {r['name']!r}: {r['operation_mode']}  "
                        f"room={r['room_temperature']}°C  out={r['outdoor_temperature']}°C  "
                        f"kWh={r['total_energy_consumed_kwh']}"
                    )
                consecutive_failures = 0

                if time.time() - last_energy_refresh_ts > energy_refresh_interval:
                    try:
                        result = melcloud_monitor.refresh_energy_history(days_back=2)
                        rows = sum(result.values()) if result else 0
                        if rows:
                            print(f"⚡ MELCloud energy refresh: {rows} hour-rows upserted")
                        last_energy_refresh_ts = time.time()
                    except Exception as e:
                        print(f"⚠️  MELCloud energy refresh failed: {e}")
            else:
                print("⚠️  MELCloud: no readings (check MELCLOUD_USER / MELCLOUD_PASS)")
                consecutive_failures += 1
        except Exception as e:
            print(f"❌ MELCloud poll error: {e}")
            consecutive_failures += 1

        delay_seconds = MELCLOUD_POLL_INTERVAL_SECONDS
        if consecutive_failures > 3:
            delay_seconds = min(600, MELCLOUD_POLL_INTERVAL_SECONDS * consecutive_failures)

        if _wait_for_shutdown(delay_seconds, stop_event):
            break


def seed_melcloud_energy(days_back: int = 30):
    """One-shot MELCloud energy backfill."""
    try:
        result = melcloud_monitor.refresh_energy_history(days_back=days_back)
        total = sum(result.values()) if result else 0
        if total:
            print(f"⚡ MELCloud backfill: {total} hour-rows across {len(result)} device(s)")
    except Exception as e:
        print(f"⚠️  MELCloud energy backfill failed: {e}")


def seed_melcloud_energy_async(days_back: int = 30):
    """Run MELCloud energy backfill without blocking startup."""
    threading.Thread(
        target=seed_melcloud_energy,
        kwargs={'days_back': days_back},
        name='seed-melcloud-energy',
        daemon=True,
    ).start()


def seed_kasa_plugs():
    """One-shot scan on startup to discover/refresh plug list."""
    try:
        found = kasa_monitor.discover_plugs()
        if found:
            kasa_monitor.upsert_plugs(KASA_DB_PATH, found)
            print(f"🔎 Kasa discovery: {len(found)} plug(s)")
        else:
            print("🔎 Kasa discovery: none found")
    except Exception as e:
        print(f"❌ Kasa discovery error: {e}")


def seed_kasa_plugs_async():
    """Run Kasa discovery without blocking startup."""
    threading.Thread(
        target=seed_kasa_plugs,
        name='seed-kasa-discovery',
        daemon=True,
    ).start()


def seed_tuya_plugs():
    """One-shot load of local tinytuya config / candidates."""
    try:
        devices = tuya_monitor.load_devices()
        tuya_monitor.upsert_plugs(KASA_DB_PATH, devices)
        if devices:
            configured = sum(1 for device in devices if device.get('local_key'))
            print(
                f"🔎 Tuya config: {len(devices)} device(s), "
                f"{configured} with local key(s)"
            )
    except Exception as e:
        print(f"❌ Tuya config load error: {e}")


def seed_tuya_plugs_async():
    """Run Tuya config load without blocking startup."""
    threading.Thread(
        target=seed_tuya_plugs,
        name='seed-tuya-config',
        daemon=True,
    ).start()


class CollectorSupervisor:
    """Keep the long-running collector workers alive inside this process."""

    WORKERS = {
        'p1': background_data_collection,
        'kasa': background_kasa_collection,
        'tuya': background_tuya_collection,
        'melcloud': background_melcloud_collection,
    }

    def __init__(self, thread_daemon: bool = True, melcloud_backfill_days: int = 30):
        self.thread_daemon = thread_daemon
        self.melcloud_backfill_days = melcloud_backfill_days
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.workers = {}
        self.watchdog_thread = None

    def _spawn_worker_locked(self, name, target):
        thread = threading.Thread(
            target=self._run_worker,
            args=(name, target),
            name=f'collector-{name}',
            daemon=self.thread_daemon,
        )
        self.workers[name] = thread
        thread.start()

    def _run_worker(self, name, target):
        try:
            target(self.stop_event)
        except Exception as e:
            print(f"❌ Collector worker {name!r} crashed: {e}")

    def _watchdog_loop(self):
        while not self.stop_event.wait(COLLECTOR_WATCHDOG_INTERVAL_SECONDS):
            with self.lock:
                if not monitoring_active:
                    break
                for name, target in self.WORKERS.items():
                    thread = self.workers.get(name)
                    if thread is None or not thread.is_alive():
                        print(f"⚠️  Restarting collector worker: {name}")
                        self._spawn_worker_locked(name, target)

    def start(self):
        global monitoring_active

        with self.lock:
            if monitoring_active and any(thread.is_alive() for thread in self.workers.values()):
                return False

            monitoring_active = True
            if self.stop_event.is_set():
                self.stop_event = threading.Event()

            self.workers = {}
            seed_kasa_plugs_async()
            seed_tuya_plugs_async()
            if self.melcloud_backfill_days > 0:
                seed_melcloud_energy_async(days_back=self.melcloud_backfill_days)

            for name, target in self.WORKERS.items():
                self._spawn_worker_locked(name, target)

            self.watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name='collector-watchdog',
                daemon=self.thread_daemon,
            )
            self.watchdog_thread.start()

        return True

    def stop(self):
        global monitoring_active

        with self.lock:
            if not monitoring_active and not self.workers:
                return False

            monitoring_active = False
            self.stop_event.set()
            threads = list(self.workers.values())
            watchdog_thread = self.watchdog_thread
            self.workers = {}
            self.watchdog_thread = None

        current = threading.current_thread()
        for thread in threads:
            if thread.is_alive() and thread is not current:
                thread.join(timeout=1)
        if watchdog_thread and watchdog_thread.is_alive() and watchdog_thread is not current:
            watchdog_thread.join(timeout=1)

        return True

    def is_running(self):
        with self.lock:
            return monitoring_active and any(thread.is_alive() for thread in self.workers.values())

    def snapshot(self):
        with self.lock:
            snapshot = {}
            for name, thread in self.workers.items():
                snapshot[name] = {
                    'alive': thread.is_alive(),
                    'name': thread.name,
                }
            return snapshot


def get_monitoring_status(max_stale_seconds: int = DEFAULT_MONITORING_STALE_SECONDS):
    """Return process and DB health for the collector runtime."""
    power_row_count = 0
    latest_power_ts = None
    latest_power_datetime = None
    db_error = None
    conn = None

    try:
        conn = sqlite3.connect(KASA_DB_PATH)
        row = conn.execute(
            'SELECT COUNT(*), MAX(timestamp), MAX(datetime) FROM power_data'
        ).fetchone()
        power_row_count = int(row[0] or 0)
        latest_power_ts = row[1]
        latest_power_datetime = row[2]
    except Exception as e:
        db_error = str(e)
    finally:
        if conn is not None:
            conn.close()

    seconds_since_power_data = None
    if latest_power_ts is not None:
        seconds_since_power_data = max(0.0, time.time() - float(latest_power_ts))

    is_stale = (
        seconds_since_power_data is None
        or seconds_since_power_data > max_stale_seconds
    )

    runtime = collector_supervisor
    return {
        'collector': {
            'running': runtime.is_running() if runtime else monitoring_active,
            'stale': is_stale,
            'max_stale_seconds': max_stale_seconds,
            'seconds_since_power_data': seconds_since_power_data,
            'latest_power_datetime': latest_power_datetime,
            'power_row_count': power_row_count,
            'db_error': db_error,
        },
        'threads': runtime.snapshot() if runtime else {},
    }


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')

@app.route('/chart')
def chart_view():
    """Full-screen chart page"""
    return render_template('chart_full.html')

@app.route('/api/latest')
def get_latest_data():
    """API endpoint for latest data"""
    if not latest_data:
        return jsonify({})

    payload = dict(latest_data)
    pricing = get_pricing_context()
    payload['spot_price'] = pricing['spot_price']
    payload['price_eur_per_kwh'] = pricing['price_eur_per_kwh']
    payload['using_fallback_price'] = pricing['using_fallback_price']
    return jsonify(payload)

@app.route('/api/spot-price')
def get_spot_price():
    """API endpoint for current Finland spot electricity price."""
    pricing = get_pricing_context()
    return jsonify({
        'spot_price': pricing['spot_price'],
        'price_eur_per_kwh': pricing['price_eur_per_kwh'],
        'using_fallback_price': pricing['using_fallback_price'],
    })

@app.route('/api/spot-price/series')
def get_spot_price_series_endpoint():
    """Return hourly spot prices overlapping a time window (for chart overlay)."""
    try:
        start_param = request.args.get('start')
        end_param = request.args.get('end')
        if not start_param or not end_param:
            return jsonify({'error': 'start and end parameters required'}), 400

        start_ts = datetime.fromisoformat(start_param.replace('Z', '+00:00')).timestamp()
        end_ts = datetime.fromisoformat(end_param.replace('Z', '+00:00')).timestamp()

        series = get_fi_spot_price_series(start_ts, end_ts)
        return jsonify({'series': series})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/history/<int:hours>')
def get_history_data(hours):
    """API endpoint for historical data"""
    try:
        cutoff_time = time.time() - (hours * 3600)
        now_ts = time.time()
        data = _build_history_payload(cutoff_time, now_ts, db_path=KASA_DB_PATH, measured_limit=1000)
        return jsonify(data)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/history/range')
def get_history_range():
    """API endpoint for historical data with custom time range"""
    try:
        start_time = request.args.get('start')
        end_time = request.args.get('end')
        
        if not start_time or not end_time:
            return jsonify({'error': 'start and end parameters required'}), 400
        
        # Convert ISO strings to timestamps
        start_timestamp = datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp()
        end_timestamp = datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp()
        
        data = _build_history_payload(start_timestamp, end_timestamp, db_path=KASA_DB_PATH, measured_limit=10000)
        return jsonify(data)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/history/summary')
def get_history_summary():
    """Summarize power, energy, and estimated cost over a selected range."""
    try:
        start_time = request.args.get('start')
        end_time = request.args.get('end')

        if not start_time or not end_time:
            return jsonify({'error': 'start and end parameters required'}), 400

        start_timestamp = parse_iso_timestamp(start_time)
        end_timestamp = parse_iso_timestamp(end_time)
        if end_timestamp <= start_timestamp:
            return jsonify({'error': 'end must be after start'}), 400

        summary = summarize_power_range(start_timestamp, end_timestamp)
        summary['start'] = start_time
        summary['end'] = end_time
        return jsonify(summary)

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices', methods=['GET'])
def api_list_invoices():
    try:
        return jsonify({'invoices': list_invoices(KASA_DB_PATH)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices/import-path', methods=['POST'])
def api_import_invoice_path():
    if not is_local_request():
        return jsonify({'error': 'Path import is only allowed from localhost'}), 403

    try:
        payload = request.get_json(silent=True) or {}
        raw_path = (payload.get('path') or '').strip()
        if not raw_path:
            return jsonify({'error': 'path is required'}), 400

        source_path = Path(raw_path).expanduser()
        if not source_path.exists():
            return jsonify({'error': 'File not found'}), 404
        if source_path.suffix.lower() != '.pdf':
            return jsonify({'error': 'Only PDF files are supported'}), 400

        pdf_sha256 = _hash_file(source_path)
        existing = _get_invoice_by_sha(pdf_sha256, KASA_DB_PATH)
        if existing and existing.get('stored_pdf_path'):
            stored_path = INVOICE_STORAGE_DIR / existing['stored_pdf_path']
            if stored_path.exists():
                parsed = parse_invoice_pdf(stored_path).to_record()
                invoice = _store_parsed_invoice(
                    parsed,
                    existing.get('source_filename') or source_path.name,
                    existing['stored_pdf_path'],
                    pdf_sha256,
                    KASA_DB_PATH,
                )
                return jsonify({'invoice': invoice, 'duplicate': True, 'refreshed': True})
            return jsonify({'invoice': _correlate_invoice(existing), 'duplicate': True, 'refreshed': False})

        safe_name = secure_filename(source_path.name) or f'invoice-{pdf_sha256[:12]}.pdf'
        stored_name = f'{int(time.time())}-{pdf_sha256[:12]}-{safe_name}'
        stored_path = INVOICE_STORAGE_DIR / stored_name
        shutil.copy2(source_path, stored_path)
        try:
            parsed = parse_invoice_pdf(stored_path).to_record()
            invoice = _store_parsed_invoice(parsed, source_path.name, stored_name, pdf_sha256, KASA_DB_PATH)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise

        return jsonify({'invoice': invoice, 'duplicate': False})
    except InvoiceParseError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices/upload', methods=['POST'])
def api_upload_invoice():
    try:
        upload = request.files.get('pdf') or request.files.get('file')
        if upload is None or not upload.filename:
            return jsonify({'error': 'pdf file is required'}), 400

        filename = secure_filename(upload.filename)
        if not filename.lower().endswith('.pdf'):
            return jsonify({'error': 'Only PDF files are supported'}), 400

        content = upload.read()
        if not content:
            return jsonify({'error': 'Uploaded file is empty'}), 400

        pdf_sha256 = _hash_bytes(content)
        existing = _get_invoice_by_sha(pdf_sha256, KASA_DB_PATH)
        if existing and existing.get('stored_pdf_path'):
            stored_path = INVOICE_STORAGE_DIR / existing['stored_pdf_path']
            if stored_path.exists():
                parsed = parse_invoice_pdf(stored_path).to_record()
                invoice = _store_parsed_invoice(
                    parsed,
                    existing.get('source_filename') or filename,
                    existing['stored_pdf_path'],
                    pdf_sha256,
                    KASA_DB_PATH,
                )
                return jsonify({'invoice': invoice, 'duplicate': True, 'refreshed': True})
            return jsonify({'invoice': _correlate_invoice(existing), 'duplicate': True, 'refreshed': False})

        stored_name = f'{int(time.time())}-{pdf_sha256[:12]}-{filename}'
        stored_path = INVOICE_STORAGE_DIR / stored_name
        stored_path.write_bytes(content)
        try:
            parsed = parse_invoice_pdf(stored_path).to_record()
            invoice = _store_parsed_invoice(parsed, filename, stored_name, pdf_sha256, KASA_DB_PATH)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise

        return jsonify({'invoice': invoice, 'duplicate': False})
    except InvoiceParseError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices/<int:invoice_id>/pdf')
def api_invoice_pdf(invoice_id: int):
    try:
        ensure_invoice_tables(KASA_DB_PATH)
        conn = sqlite3.connect(KASA_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT source_filename, stored_pdf_path FROM electricity_invoices WHERE id = ?',
                (invoice_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None or not row['stored_pdf_path']:
            return jsonify({'error': 'Invoice PDF not found'}), 404

        stored_name = Path(row['stored_pdf_path']).name
        return send_from_directory(INVOICE_STORAGE_DIR, stored_name, as_attachment=False, download_name=row['source_filename'])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices/<int:invoice_id>', methods=['DELETE'])
def api_delete_invoice(invoice_id: int):
    try:
        ensure_invoice_tables(KASA_DB_PATH)
        conn = sqlite3.connect(KASA_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT stored_pdf_path FROM electricity_invoices WHERE id = ?',
                (invoice_id,),
            ).fetchone()
            if row is None:
                return jsonify({'error': 'Invoice not found'}), 404

            stored_pdf_path = row['stored_pdf_path']
            conn.execute('DELETE FROM electricity_invoices WHERE id = ?', (invoice_id,))
            conn.commit()

            if stored_pdf_path:
                remaining = conn.execute(
                    'SELECT COUNT(*) FROM electricity_invoices WHERE stored_pdf_path = ?',
                    (stored_pdf_path,),
                ).fetchone()[0]
            else:
                remaining = 0
        finally:
            conn.close()

        if stored_pdf_path and remaining == 0:
            (INVOICE_STORAGE_DIR / Path(stored_pdf_path).name).unlink(missing_ok=True)

        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/markers', methods=['GET'])
def get_markers():
    """Get chart markers within time range"""
    try:
        start_time = request.args.get('start')
        end_time = request.args.get('end')
        
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        
        if start_time and end_time:
            # Convert ISO strings to timestamps
            start_timestamp = datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp()
            end_timestamp = datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp()
            
            cursor.execute('''
                SELECT * FROM chart_markers 
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            ''', (start_timestamp, end_timestamp))
        else:
            # Get all markers
            cursor.execute('''
                SELECT * FROM chart_markers 
                ORDER BY timestamp DESC
            ''')
        
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        conn.close()
        
        markers = [dict(zip(columns, row)) for row in rows]
        return jsonify(markers)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/markers', methods=['POST'])
def add_marker():
    """Add a new chart marker"""
    try:
        data = request.json
        timestamp = data.get('timestamp')
        label = data.get('label', '')
        description = data.get('description', '')
        color = data.get('color', '#ff6b6b')
        
        if not timestamp:
            return jsonify({'error': 'timestamp is required'}), 400
        
        dt = datetime.fromtimestamp(timestamp)
        created_at = time.time()
        
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chart_markers (timestamp, datetime, label, description, color, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (timestamp, dt.isoformat(), label, description, color, created_at))
        
        marker_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({
            'id': marker_id,
            'timestamp': timestamp,
            'datetime': dt.isoformat(),
            'label': label,
            'description': description,
            'color': color,
            'created_at': created_at
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/markers/<int:marker_id>', methods=['DELETE'])
def delete_marker(marker_id):
    """Delete a chart marker"""
    try:
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM chart_markers WHERE id = ?', (marker_id,))
        
        if cursor.rowcount == 0:
            return jsonify({'error': 'Marker not found'}), 404
        
        conn.commit()
        conn.close()
        
        return jsonify({'message': 'Marker deleted successfully'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats/<int:hours>')
def get_stats(hours):
    """API endpoint for statistics"""
    try:
        cutoff_time = time.time() - (hours * 3600)
        
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                AVG(total_power_w) as avg_power,
                MIN(total_power_w) as min_power,
                MAX(total_power_w) as max_power,
                AVG(power_l1_w) as avg_l1,
                AVG(power_l2_w) as avg_l2,
                AVG(power_l3_w) as avg_l3,
                AVG(current_total_a) as avg_current,
                COUNT(*) as data_points
            FROM power_data 
            WHERE timestamp > ?
        ''', (cutoff_time,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            pricing = get_pricing_context()
            avg_power = round(result[0] or 0, 0)
            stats = {
                'avg_power': avg_power,
                'min_power': round(result[1] or 0, 0),
                'max_power': round(result[2] or 0, 0),
                'avg_l1': round(result[3] or 0, 0),
                'avg_l2': round(result[4] or 0, 0),
                'avg_l3': round(result[5] or 0, 0),
                'avg_current': round(result[6] or 0, 1),
                'data_points': result[7] or 0,
                'hours': hours,
                'spot_price': pricing['spot_price'],
                'price_eur_per_kwh': pricing['price_eur_per_kwh'],
                'using_fallback_price': pricing['using_fallback_price'],
            }
            
            # Calculate imbalance
            total_phases = stats['avg_l1'] + stats['avg_l2'] + stats['avg_l3']
            if total_phases > 0:
                stats['imbalance'] = round(max(stats['avg_l1'], stats['avg_l2'], stats['avg_l3']) - 
                                         min(stats['avg_l1'], stats['avg_l2'], stats['avg_l3']), 0)
            else:
                stats['imbalance'] = 0

            hourly_cost = avg_power * pricing['price_eur_per_kwh'] / 1000
            stats['est_hourly_cost_eur'] = round(hourly_cost, 3)
            stats['est_daily_cost_eur'] = round(hourly_cost * 24, 2)
            stats['est_monthly_cost_eur'] = round(hourly_cost * 24 * 30, 2)
                
            return jsonify(stats)
        else:
            return jsonify({'error': 'No data available'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _build_chat_snapshot():
    """Compact live snapshot used as volatile user context for the agent."""
    pricing = get_pricing_context()
    snapshot = {
        "now": datetime.now().isoformat(timespec="seconds"),
        "pricing": {
            "price_eur_per_kwh": pricing["price_eur_per_kwh"],
            "using_fallback_price": pricing["using_fallback_price"],
            "spot_price": pricing["spot_price"],
        },
    }
    if latest_data:
        snapshot["p1_latest"] = {
            k: latest_data.get(k) for k in (
                "active_power_w",
                "active_power_l1_w", "active_power_l2_w", "active_power_l3_w",
                "active_current_a",
                "active_voltage_l1_v", "active_voltage_l2_v", "active_voltage_l3_v",
                "total_power_import_kwh", "total_power_export_kwh",
                "timestamp",
            )
            if latest_data.get(k) is not None
        }
    plugs_latest = get_combined_latest_plugs()
    if plugs_latest:
        snapshot["plugs_latest"] = [
            {
                "alias": r.get("alias"),
                "mac": r.get("mac"),
                "source": r.get("source"),
                "power_w": r.get("power_w"),
                "is_on": r.get("is_on"),
                "total_kwh": r.get("total_kwh"),
                "timestamp": r.get("timestamp"),
            }
            for r in plugs_latest
        ]
    if latest_melcloud_by_device:
        snapshot["heat_pumps_latest"] = [
            {
                "name": r.get("name"),
                "device_id": r.get("device_id"),
                "power": bool(r.get("power")),
                "operation_mode": r.get("operation_mode"),
                "room_temperature": r.get("room_temperature"),
                "target_temperature": r.get("target_temperature"),
                "outdoor_temperature": r.get("outdoor_temperature"),
                "total_energy_consumed_kwh": r.get("total_energy_consumed_kwh"),
                "fan_speed": r.get("fan_speed"),
                "timestamp": r.get("timestamp"),
            }
            for r in latest_melcloud_by_device.values()
        ]
    return snapshot


@app.route('/api/chat', methods=['POST'])
def chat_with_pi():
    """Agentic chat backed by Claude Opus 4.7 with SQL/HTTP/web_search tools."""
    try:
        import re
        import uuid as _uuid
        payload = request.json or {}
        user_message = (payload.get('message') or '').strip()
        raw_sid = payload.get('session_id') or ''
        # Claude CLI's --session-id requires a UUID; coerce stable non-UUIDs via hash
        if re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', raw_sid):
            session_id = raw_sid.lower()
        elif raw_sid:
            session_id = str(_uuid.uuid5(_uuid.NAMESPACE_OID, raw_sid))
        else:
            session_id = 'default'
        reset = bool(payload.get('reset'))

        if not user_message:
            return jsonify({'error': 'message is required'}), 400
        if reset:
            chat_histories.pop(session_id, None)

        snapshot = _build_chat_snapshot()

        # Agent path
        if power_agent.available:
            history = chat_histories.get(session_id, [])
            result = power_agent.chat(
                user_message=user_message,
                context_snapshot=snapshot,
                history=history,
                session_id=session_id,
            )

            # Persist rolling history (cap to N turns to keep prompts bounded)
            new_user_turn = {
                "role": "user",
                "content": [{"type": "text", "text": user_message}],
            }
            new_assistant_turn = {
                "role": "assistant",
                "content": [{"type": "text", "text": result.get("response", "")}],
            }
            history = history + [new_user_turn, new_assistant_turn]
            if len(history) > CHAT_HISTORY_MAX_TURNS * 2:
                history = history[-CHAT_HISTORY_MAX_TURNS * 2:]
            chat_histories[session_id] = history

            return jsonify({
                'response': result.get('response'),
                'agent': 'claude',
                'iterations': result.get('iterations'),
                'tool_calls': result.get('tool_calls'),
                'usage': result.get('usage'),
            })

        # Fallback: old keyword templates (no API key configured)
        fallback_ctx = dict(latest_data or {})
        fallback_ctx.update({
            'spot_price': snapshot['pricing']['spot_price'],
            'price_eur_per_kwh': snapshot['pricing']['price_eur_per_kwh'],
            'using_fallback_price': snapshot['pricing']['using_fallback_price'],
        })
        text = pi_chat.chat_with_pi(user_message, fallback_ctx)
        return jsonify({
            'response': text,
            'agent': 'fallback_templates',
            'hint': f'Set CLAUDE_API_KEY for agentic responses ({power_agent.init_error}).',
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# get_contextual_response function removed - now handled by PiAgentChat

@app.route('/api/kasa/plugs', methods=['GET'])
def api_kasa_plugs():
    """List all known smart plugs with latest reading snapshot."""
    try:
        return jsonify({'plugs': get_combined_latest_plugs()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tuya/plugs', methods=['GET'])
def api_tuya_plugs():
    """List all known Tuya / Nedis plugs, including candidates awaiting keys."""
    try:
        return jsonify({'plugs': tuya_monitor.get_latest_readings(KASA_DB_PATH)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kasa/discover', methods=['POST'])
def api_kasa_discover():
    """Trigger a fresh LAN scan for Kasa plugs."""
    try:
        found = kasa_monitor.discover_plugs()
        kasa_monitor.upsert_plugs(KASA_DB_PATH, found)
        return jsonify({'found': len(found), 'plugs': found})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kasa/history/<int:hours>')
def api_kasa_history_hours(hours):
    """Per-plug readings for the last N hours, grouped by mac."""
    try:
        mac = request.args.get('mac')
        end_ts = time.time()
        start_ts = end_ts - hours * 3600
        rows = kasa_monitor.get_history(KASA_DB_PATH, start_ts, end_ts, mac=mac)
        return jsonify({'readings': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kasa/breakdown/range')
def api_kasa_breakdown_range():
    """Aligned per-plug + 'unaccounted' series over a time range.

    For each minute-bucket of P1 power_data, find the most-recent kasa reading
    per plug (within a 5-minute lookback) and compute the leftover as
    P1_total - sum(plug_power). Result is stack-friendly for Chart.js.
    """
    try:
        start = request.args.get('start')
        end = request.args.get('end')
        if not start or not end:
            return jsonify({'error': 'start and end parameters required'}), 400
        start_ts = datetime.fromisoformat(start.replace('Z', '+00:00')).timestamp()
        end_ts = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp()
        return jsonify(_build_breakdown(start_ts, end_ts))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kasa/breakdown/<int:hours>')
def api_kasa_breakdown_hours(hours):
    try:
        end_ts = time.time()
        start_ts = end_ts - hours * 3600
        return jsonify(_build_breakdown(start_ts, end_ts))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _build_breakdown(start_ts, end_ts):
    """Align smart-plug readings to P1 timestamps; return plugs + unaccounted series."""
    plug_meta = [
        meta for meta in get_combined_plug_metadata()
        if meta.get('enabled', True) and not meta.get('needs_setup')
    ]
    readings = get_combined_plug_history(start_ts - 300, end_ts)

    # Group plug readings by plug_id, sorted by timestamp
    per_plug = {}
    for r in readings:
        per_plug.setdefault(r['plug_id'], []).append(r)
    for lst in per_plug.values():
        lst.sort(key=lambda x: x['timestamp'])

    # Fetch P1 power samples in range
    conn = sqlite3.connect(KASA_DB_PATH)
    try:
        cursor = conn.execute(
            '''SELECT timestamp, datetime, total_power_w FROM power_data
               WHERE timestamp BETWEEN ? AND ?
               ORDER BY timestamp ASC LIMIT 20000''',
            (start_ts, end_ts),
        )
        p1_rows = cursor.fetchall()
    finally:
        conn.close()

    times = [row[1] for row in p1_rows]
    totals = [row[2] or 0 for row in p1_rows]

    # For each plug, build a value-per-timestamp array using last-observation-carried-forward
    LOOKBACK_SECONDS = 300
    plug_series = []
    for meta in plug_meta:
        plug_id = meta['plug_id']
        values = []
        entries = per_plug.get(plug_id, [])
        idx = 0
        last_value = 0.0
        last_ts = -1e9
        for (ts, _dt, _w) in p1_rows:
            while idx < len(entries) and entries[idx]['timestamp'] <= ts:
                last_value = entries[idx].get('power_w') or 0.0
                last_ts = entries[idx]['timestamp']
                idx += 1
            if ts - last_ts > LOOKBACK_SECONDS:
                values.append(0.0)
            else:
                values.append(last_value)
        plug_series.append({
            'plug_id': plug_id,
            'mac': meta.get('mac'),
            'alias': meta.get('alias') or meta.get('name') or plug_id,
            'source': meta.get('source'),
            'source_label': meta.get('source_label'),
            'values': values,
        })

    unaccounted = []
    for i, total in enumerate(totals):
        plug_sum = sum(series['values'][i] for series in plug_series)
        unaccounted.append(max(total - plug_sum, 0))

    return {
        'times': times,
        'total': totals,
        'plugs': plug_series,
        'unaccounted': unaccounted,
    }


@app.route('/api/kasa/history/range')
def api_kasa_history_range():
    try:
        start = request.args.get('start')
        end = request.args.get('end')
        mac = request.args.get('mac')
        if not start or not end:
            return jsonify({'error': 'start and end parameters required'}), 400
        start_ts = datetime.fromisoformat(start.replace('Z', '+00:00')).timestamp()
        end_ts = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp()
        rows = kasa_monitor.get_history(KASA_DB_PATH, start_ts, end_ts, mac=mac)
        return jsonify({'readings': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/melcloud/devices', methods=['GET'])
def api_melcloud_devices():
    """List MELCloud heat pumps with their most recent reading."""
    try:
        devices = enrich_melcloud_readings(
            melcloud_monitor.list_devices_with_latest(KASA_DB_PATH),
            include_estimated_power=True,
        )
        return jsonify({'devices': devices})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/melcloud/power-series')
def api_melcloud_power_series():
    """Hourly average ILP power (W) derived from MELCloud hourly kWh rows."""
    try:
        start_ts, end_ts, window = resolve_time_window(default_hours=168)
        device_id = request.args.get('device_id', type=int)

        conn = sqlite3.connect(KASA_DB_PATH)
        try:
            if device_id:
                rows = conn.execute(
                    '''
                    SELECT e.device_id,
                           COALESCE(d.name, '') AS name,
                           e.hour_start_ts,
                           e.hour_start_iso,
                           e.total_kwh,
                           e.coverage_percent
                    FROM melcloud_energy_hourly e
                    LEFT JOIN melcloud_devices d ON d.device_id = e.device_id
                    WHERE e.device_id = ?
                      AND e.hour_start_ts < ?
                      AND (e.hour_start_ts + 3600) > ?
                    ORDER BY e.hour_start_ts ASC
                    ''',
                    (device_id, end_ts, start_ts),
                ).fetchall()
            else:
                rows = conn.execute(
                    '''
                    SELECT e.device_id,
                           COALESCE(d.name, '') AS name,
                           e.hour_start_ts,
                           e.hour_start_iso,
                           e.total_kwh,
                           e.coverage_percent
                    FROM melcloud_energy_hourly e
                    LEFT JOIN melcloud_devices d ON d.device_id = e.device_id
                    WHERE e.hour_start_ts < ?
                      AND (e.hour_start_ts + 3600) > ?
                    ORDER BY name ASC, e.hour_start_ts ASC
                    ''',
                    (end_ts, start_ts),
                ).fetchall()
        finally:
            conn.close()

        by_device = {}
        for row in rows:
            dev_id = row[0]
            name = row[1] or f'ILP {dev_id}'
            scope = get_melcloud_scope(dev_id, name)
            series = by_device.setdefault(
                dev_id,
                {
                    'device_id': dev_id,
                    'name': name,
                    'included_in_p1': scope['included_in_p1'],
                    'scope_label': scope['scope_label'],
                    'points': [],
                },
            )
            bucket_start_ts = float(row[2])
            bucket_end_ts = bucket_start_ts + 3600
            clipped_start_ts = max(bucket_start_ts, start_ts)
            clipped_end_ts = min(bucket_end_ts, end_ts)
            if clipped_end_ts <= clipped_start_ts:
                continue
            coverage = float(row[5]) if row[5] is not None else None
            avg_power_w = round(float(row[4] or 0.0) * 1000.0, 1)
            series['points'].append(
                {
                    'start': datetime.fromtimestamp(clipped_start_ts).isoformat(),
                    'end': datetime.fromtimestamp(clipped_end_ts).isoformat(),
                    'avg_power_w': avg_power_w,
                    'coverage_percent': coverage,
                    'partial': bool(coverage is not None and coverage < 80.0),
                }
            )

        ordered = sorted(
            by_device.values(),
            key=lambda item: (
                0 if item['included_in_p1'] is True else 1,
                item['name'].lower(),
            ),
        )
        return jsonify(
            {
                'series': ordered,
                'start': window['start'],
                'end': window['end'],
                'hours': window['hours'],
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/melcloud/energy')
def api_melcloud_energy():
    """Hourly per-mode energy from MELCloud Reports API.

    Query params:
      hours     — window size (default 168 = 7 days)
      device_id — optional filter
      aggregate — 'hourly' (default) or 'daily'
    """
    try:
        hours = request.args.get('hours', 168, type=int)
        device_id = request.args.get('device_id', type=int)
        aggregate = request.args.get('aggregate', 'hourly')
        start_ts = time.time() - hours * 3600

        conn = sqlite3.connect(KASA_DB_PATH)
        try:
            if aggregate == 'daily':
                if device_id:
                    rows = conn.execute('''
                        SELECT device_id,
                               substr(hour_start_iso, 1, 10) AS date,
                               ROUND(SUM(heating_kwh), 3) AS heating_kwh,
                               ROUND(SUM(cooling_kwh), 3) AS cooling_kwh,
                               ROUND(SUM(dry_kwh), 3)     AS dry_kwh,
                               ROUND(SUM(fan_kwh), 3)     AS fan_kwh,
                               ROUND(SUM(auto_kwh), 3)    AS auto_kwh,
                               ROUND(SUM(other_kwh), 3)   AS other_kwh,
                               ROUND(SUM(total_kwh), 3)   AS total_kwh,
                               ROUND(AVG(coverage_percent), 1) AS coverage_pct,
                               COUNT(*) AS hours_counted
                        FROM melcloud_energy_hourly
                        WHERE device_id = ? AND hour_start_ts >= ?
                        GROUP BY device_id, date
                        ORDER BY date ASC
                    ''', (device_id, start_ts)).fetchall()
                else:
                    rows = conn.execute('''
                        SELECT device_id,
                               substr(hour_start_iso, 1, 10) AS date,
                               ROUND(SUM(heating_kwh), 3) AS heating_kwh,
                               ROUND(SUM(cooling_kwh), 3) AS cooling_kwh,
                               ROUND(SUM(dry_kwh), 3)     AS dry_kwh,
                               ROUND(SUM(fan_kwh), 3)     AS fan_kwh,
                               ROUND(SUM(auto_kwh), 3)    AS auto_kwh,
                               ROUND(SUM(other_kwh), 3)   AS other_kwh,
                               ROUND(SUM(total_kwh), 3)   AS total_kwh,
                               ROUND(AVG(coverage_percent), 1) AS coverage_pct,
                               COUNT(*) AS hours_counted
                        FROM melcloud_energy_hourly
                        WHERE hour_start_ts >= ?
                        GROUP BY device_id, date
                        ORDER BY date ASC, device_id
                    ''', (start_ts,)).fetchall()
                cols = ['device_id', 'date', 'heating_kwh', 'cooling_kwh', 'dry_kwh',
                        'fan_kwh', 'auto_kwh', 'other_kwh', 'total_kwh',
                        'coverage_pct', 'hours_counted']
            else:
                if device_id:
                    rows = conn.execute('''
                        SELECT device_id, hour_start_ts, hour_start_iso,
                               heating_kwh, cooling_kwh, dry_kwh, fan_kwh,
                               auto_kwh, other_kwh, total_kwh, coverage_percent
                        FROM melcloud_energy_hourly
                        WHERE device_id = ? AND hour_start_ts >= ?
                        ORDER BY hour_start_ts ASC
                    ''', (device_id, start_ts)).fetchall()
                else:
                    rows = conn.execute('''
                        SELECT device_id, hour_start_ts, hour_start_iso,
                               heating_kwh, cooling_kwh, dry_kwh, fan_kwh,
                               auto_kwh, other_kwh, total_kwh, coverage_percent
                        FROM melcloud_energy_hourly
                        WHERE hour_start_ts >= ?
                        ORDER BY hour_start_ts ASC
                    ''', (start_ts,)).fetchall()
                cols = ['device_id', 'hour_start_ts', 'hour_start_iso',
                        'heating_kwh', 'cooling_kwh', 'dry_kwh', 'fan_kwh',
                        'auto_kwh', 'other_kwh', 'total_kwh', 'coverage_percent']
        finally:
            conn.close()

        return jsonify({
            'rows': [dict(zip(cols, r)) for r in rows],
            'aggregate': aggregate,
            'hours': hours,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/melcloud/energy/refresh', methods=['POST'])
def api_melcloud_energy_refresh():
    """Force a fresh backfill from MELCloud (synchronous, blocks until done)."""
    try:
        days = int(request.args.get('days', 7))
        result = melcloud_monitor.refresh_energy_history(days_back=days)
        return jsonify({'upserted_rows': result, 'days': days})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/melcloud/history/<int:hours>')
def api_melcloud_history(hours):
    """Per-device MELCloud readings for the last N hours."""
    try:
        device_id = request.args.get('device_id', type=int)
        start_ts = time.time() - hours * 3600
        conn = sqlite3.connect(KASA_DB_PATH)
        try:
            if device_id:
                rows = conn.execute(
                    '''SELECT timestamp, datetime, device_id, name, power, operation_mode,
                              room_temperature, target_temperature, outdoor_temperature,
                              total_energy_consumed_kwh, wifi_signal_dbm
                       FROM melcloud_readings
                       WHERE device_id = ? AND timestamp >= ?
                       ORDER BY timestamp ASC LIMIT 20000''',
                    (device_id, start_ts),
                ).fetchall()
            else:
                rows = conn.execute(
                    '''SELECT timestamp, datetime, device_id, name, power, operation_mode,
                              room_temperature, target_temperature, outdoor_temperature,
                              total_energy_consumed_kwh, wifi_signal_dbm
                       FROM melcloud_readings
                       WHERE timestamp >= ?
                       ORDER BY timestamp ASC LIMIT 50000''',
                    (start_ts,),
                ).fetchall()
        finally:
            conn.close()
        cols = ['timestamp', 'datetime', 'device_id', 'name', 'power', 'operation_mode',
                'room_temperature', 'target_temperature', 'outdoor_temperature',
                'total_energy_consumed_kwh', 'wifi_signal_dbm']
        return jsonify({'readings': [dict(zip(cols, r)) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/start_monitoring')
def start_monitoring():
    """Start background data collection"""
    started, _runtime = start_collection_runtime(thread_daemon=True)
    return jsonify({
        'status': 'Monitoring started' if started else 'Monitoring already active',
        'details': get_monitoring_status(),
    })

@app.route('/stop_monitoring')
def stop_monitoring():
    """Stop background data collection"""
    stopped = stop_collection_runtime()
    return jsonify({
        'status': 'Monitoring stopped' if stopped else 'Monitoring already stopped',
        'details': get_monitoring_status(),
    })


@app.route('/api/monitoring/status')
def api_monitoring_status():
    """Collector health status for dashboards and service checks."""
    stale_seconds = request.args.get('stale_seconds', DEFAULT_MONITORING_STALE_SECONDS, type=int)
    return jsonify(get_monitoring_status(max_stale_seconds=stale_seconds))

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    emit('status', {'message': 'Connected to P1 Monitor'})
    
    # Send latest data if available
    if latest_data:
        emit('new_data', latest_data)
    combined_plugs = get_combined_latest_plugs()
    if combined_plugs:
        emit('kasa_data', {'readings': combined_plugs})
    if latest_melcloud_by_device:
        emit('melcloud_data', {'readings': list(latest_melcloud_by_device.values())})


def start_collection_runtime(thread_daemon: bool = True, melcloud_backfill_days: int = 30):
    """Start the background collectors under a small in-process supervisor."""
    global collector_supervisor

    if collector_supervisor is None or collector_supervisor.stop_event.is_set():
        collector_supervisor = CollectorSupervisor(
            thread_daemon=thread_daemon,
            melcloud_backfill_days=melcloud_backfill_days,
        )

    started = collector_supervisor.start()
    return started, collector_supervisor


def stop_collection_runtime():
    """Stop the collector supervisor, if it is running."""
    runtime = collector_supervisor
    if runtime is None:
        return False
    return runtime.stop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Run the HomeWizard dashboard, collector, or both.',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        '--collector-only',
        action='store_true',
        help='Run the data collectors without starting the Flask dashboard.',
    )
    mode.add_argument(
        '--web-only',
        action='store_true',
        help='Run the Flask dashboard without starting embedded collectors.',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=int(os.environ.get('PORT', 5001)),
        help='Dashboard port when running the Flask app.',
    )
    parser.add_argument(
        '--melcloud-backfill-days',
        type=int,
        default=30,
        help='How many MELCloud days to backfill on collector startup.',
    )
    return parser.parse_args(argv)


def install_signal_handlers():
    def handle_shutdown(signum, _frame):
        signal_name = signal.Signals(signum).name
        print(f"\n🛑 Received {signal_name}, stopping HomeWizard monitor...")
        stop_collection_runtime()
        raise SystemExit(0)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_shutdown)


def main(argv=None):
    args = parse_args(argv)
    run_web = not args.collector_only
    run_collectors = not args.web_only

    install_signal_handlers()

    print("🚀 Starting HomeWizard P1 Web Monitor...")
    if run_web:
        print(f"📊 Dashboard will be available at: http://localhost:{args.port}")
    if run_collectors:
        start_collection_runtime(
            thread_daemon=True,
            melcloud_backfill_days=args.melcloud_backfill_days,
        )
    else:
        print("📡 Web-only mode active; embedded collectors are disabled.")

    if not run_web:
        print("🔁 Collector-only mode active; data gathering will continue without the UI.")

    try:
        if run_web:
            socketio.run(
                app,
                host='0.0.0.0',
                port=args.port,
                debug=False,
                allow_unsafe_werkzeug=True,
            )
        else:
            while True:
                time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        stop_collection_runtime()


if __name__ == '__main__':
    main()
