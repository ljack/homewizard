#!/usr/bin/env python3
"""
HomeWizard P1 Web Monitor with PI Agent Chat Integration
Flask-based web interface for real-time monitoring and AI analysis
"""

from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import json
import secrets
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
from chat_agent import PowerAnalysisAgent

app = Flask(__name__)
app.config['SECRET_KEY'] = (
    os.environ.get('FLASK_SECRET_KEY')
    or os.environ.get('SECRET_KEY')
    or secrets.token_hex(32)
)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
socketio = SocketIO(app, cors_allowed_origins="*")

# Global variables
data_collector = None
kasa_collector = None
melcloud_collector = None
latest_data = {}
latest_kasa_by_mac = {}
latest_melcloud_by_device = {}
monitoring_active = False
MELCLOUD_POLL_INTERVAL_SECONDS = 120
pi_chat = PiAgentChat()  # legacy fallback (keyword templates, no API key needed)
power_agent = PowerAnalysisAgent()  # agentic Claude-backed analyzer
chat_histories = {}  # session_id -> list[message dict], bounded per conversation
CHAT_HISTORY_MAX_TURNS = 12
DEFAULT_ELECTRICITY_PRICE_EUR_PER_KWH = 0.25
KASA_POLL_INTERVAL_SECONDS = 60
KASA_DB_PATH = 'p1_data.db'


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

        # Mitsubishi MELCloud ILP state + lifetime energy
        melcloud_monitor.ensure_melcloud_tables(KASA_DB_PATH)
    
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

def background_data_collection():
    """Background thread for continuous data collection"""
    global latest_data, monitoring_active
    
    monitor = WebP1Monitor()
    
    while monitoring_active:
        try:
            data = monitor.fetch_data()
            if data:
                # Store to both CSV and database
                monitor.store_data(data)
                monitor.store_data_db(data)
                
                # Update latest data
                pricing = get_pricing_context()
                latest_data = dict(data)
                latest_data['timestamp'] = datetime.now().isoformat()
                latest_data['spot_price'] = pricing['spot_price']
                latest_data['price_eur_per_kwh'] = pricing['price_eur_per_kwh']
                latest_data['using_fallback_price'] = pricing['using_fallback_price']
                
                # Emit to connected clients
                socketio.emit('new_data', latest_data)
                
                print(f"✅ Data collected: {data.get('active_power_w', 0)}W")
            else:
                print("❌ Failed to fetch data")
                
        except Exception as e:
            print(f"❌ Error in data collection: {e}")
        
        time.sleep(60)  # Collect every minute


def background_kasa_collection():
    """Poll all enabled Kasa plugs, persist readings, broadcast via socket."""
    global latest_kasa_by_mac
    while monitoring_active:
        try:
            readings = kasa_monitor.poll_plugs(KASA_DB_PATH)
            if readings:
                by_mac = {r['mac']: r for r in readings}
                latest_kasa_by_mac = by_mac
                socketio.emit('kasa_data', {'readings': readings})
                total = sum(r['power_w'] for r in readings)
                print(f"🔌 Kasa poll: {len(readings)} plug(s), total {total:.1f}W")
        except Exception as e:
            print(f"❌ Kasa poll error: {e}")
        time.sleep(KASA_POLL_INTERVAL_SECONDS)


def background_melcloud_collection():
    """Poll MELCloud every ~2 min; refresh hourly energy reports every ~1h."""
    global latest_melcloud_by_device
    consecutive_failures = 0
    last_energy_refresh_ts = 0.0
    ENERGY_REFRESH_INTERVAL = 3600  # 1 hour

    while monitoring_active:
        try:
            readings = melcloud_monitor.poll_melcloud(KASA_DB_PATH)
            if readings:
                latest_melcloud_by_device = {r['device_id']: r for r in readings}
                socketio.emit('melcloud_data', {'readings': readings})
                for r in readings:
                    print(
                        f"🔥 MELCloud {r['name']!r}: {r['operation_mode']}  "
                        f"room={r['room_temperature']}°C  out={r['outdoor_temperature']}°C  "
                        f"kWh={r['total_energy_consumed_kwh']}"
                    )
                consecutive_failures = 0

                # Hourly energy report refresh (only when state poll succeeded)
                if time.time() - last_energy_refresh_ts > ENERGY_REFRESH_INTERVAL:
                    try:
                        # Refresh only last 48h on the hourly tick; initial backfill
                        # is handled by the startup seeder below.
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

        if consecutive_failures > 3:
            time.sleep(min(600, MELCLOUD_POLL_INTERVAL_SECONDS * consecutive_failures))
        else:
            time.sleep(MELCLOUD_POLL_INTERVAL_SECONDS)


def seed_melcloud_energy_async(days_back: int = 30):
    """One-shot backfill on startup. Non-blocking."""
    def run():
        try:
            result = melcloud_monitor.refresh_energy_history(days_back=days_back)
            total = sum(result.values()) if result else 0
            if total:
                print(f"⚡ MELCloud backfill: {total} hour-rows across {len(result)} device(s)")
        except Exception as e:
            print(f"⚠️  MELCloud energy backfill failed: {e}")
    threading.Thread(target=run, daemon=True).start()


def seed_kasa_plugs_async():
    """One-shot scan on startup to discover/refresh plug list. Non-blocking."""
    def run():
        try:
            found = kasa_monitor.discover_plugs()
            if found:
                kasa_monitor.upsert_plugs(KASA_DB_PATH, found)
                print(f"🔎 Kasa discovery: {len(found)} plug(s)")
            else:
                print("🔎 Kasa discovery: none found")
        except Exception as e:
            print(f"❌ Kasa discovery error: {e}")
    threading.Thread(target=run, daemon=True).start()


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
        
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM power_data 
            WHERE timestamp > ? 
            ORDER BY timestamp DESC 
            LIMIT 1000
        ''', (cutoff_time,))
        
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        conn.close()
        
        data = [dict(zip(columns, row)) for row in rows]
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
        
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM power_data 
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC 
            LIMIT 10000
        ''', (start_timestamp, end_timestamp))
        
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        conn.close()
        
        data = [dict(zip(columns, row)) for row in rows]
        return jsonify(data)
        
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
    if latest_kasa_by_mac:
        snapshot["plugs_latest"] = [
            {
                "alias": r.get("alias"),
                "mac": r.get("mac"),
                "power_w": r.get("power_w"),
                "is_on": r.get("is_on"),
                "total_kwh": r.get("total_kwh"),
                "timestamp": r.get("timestamp"),
            }
            for r in latest_kasa_by_mac.values()
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
    """List all known Kasa plugs with latest reading snapshot."""
    try:
        return jsonify({'plugs': kasa_monitor.get_latest_readings(KASA_DB_PATH)})
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
    """Align kasa readings to P1 timestamps; return plugs + unaccounted series."""
    plug_meta = kasa_monitor.list_plugs(KASA_DB_PATH)
    readings = kasa_monitor.get_history(KASA_DB_PATH, start_ts - 300, end_ts, mac=None)

    # Group plug readings by mac, sorted by timestamp
    per_plug = {}
    for r in readings:
        per_plug.setdefault(r['mac'], []).append(r)
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
    plug_idx = {mac: 0 for mac in per_plug}
    for meta in plug_meta:
        mac = meta['mac']
        values = []
        entries = per_plug.get(mac, [])
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
            'mac': mac,
            'alias': meta.get('alias') or mac,
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
        devices = melcloud_monitor.list_devices_with_latest(KASA_DB_PATH)
        # Add estimated current W for each (rolling 20 min)
        for d in devices:
            d['estimated_power_w'] = melcloud_monitor.estimate_current_power_w(
                d['device_id'], KASA_DB_PATH,
            )
        return jsonify({'devices': devices})
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
    global data_collector, kasa_collector, monitoring_active

    global melcloud_collector
    if not monitoring_active:
        monitoring_active = True
        data_collector = threading.Thread(target=background_data_collection, daemon=True)
        data_collector.start()
        kasa_collector = threading.Thread(target=background_kasa_collection, daemon=True)
        kasa_collector.start()
        melcloud_collector = threading.Thread(target=background_melcloud_collection, daemon=True)
        melcloud_collector.start()
        return jsonify({'status': 'Monitoring started'})
    else:
        return jsonify({'status': 'Monitoring already active'})

@app.route('/stop_monitoring')
def stop_monitoring():
    """Stop background data collection"""
    global monitoring_active
    monitoring_active = False
    return jsonify({'status': 'Monitoring stopped'})

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    emit('status', {'message': 'Connected to P1 Monitor'})
    
    # Send latest data if available
    if latest_data:
        emit('new_data', latest_data)
    if latest_kasa_by_mac:
        emit('kasa_data', {'readings': list(latest_kasa_by_mac.values())})
    if latest_melcloud_by_device:
        emit('melcloud_data', {'readings': list(latest_melcloud_by_device.values())})

if __name__ == '__main__':
    print("🚀 Starting HomeWizard P1 Web Monitor...")
    print("📊 Dashboard will be available at: http://localhost:5001")
    
    # Auto-start monitoring
    monitoring_active = True
    data_collector = threading.Thread(target=background_data_collection, daemon=True)
    data_collector.start()

    # Auto-discover Kasa plugs in background, then start polling them
    seed_kasa_plugs_async()
    kasa_collector = threading.Thread(target=background_kasa_collection, daemon=True)
    kasa_collector.start()

    # Start MELCloud polling (heat pumps) if credentials are configured
    melcloud_collector = threading.Thread(target=background_melcloud_collection, daemon=True)
    melcloud_collector.start()
    # Backfill last 30 days of hourly energy once on boot (cheap: ~2 API calls per 2-day segment)
    seed_melcloud_energy_async(days_back=30)

    # Start web server
    socketio.run(app, host='0.0.0.0', port=5001, debug=False, allow_unsafe_werkzeug=True)
