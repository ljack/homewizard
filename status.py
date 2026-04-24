#!/usr/bin/env python3
"""
Quick status check for the HomeWizard monitor.
"""

import argparse
from datetime import datetime
import json
import os
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


DEFAULT_METER_URL = os.environ.get('P1_METER_URL', 'http://homewizard.local')
DEFAULT_WEB_URL = os.environ.get('HOMEWIZARD_WEB_URL', 'http://localhost:5001')
DEFAULT_DB_PATH = os.environ.get('HOMEWIZARD_DB_PATH', 'p1_data.db')
DEFAULT_STALE_SECONDS = 180


def http_get_json(url: str, timeout: int = 5):
    with urlopen(url, timeout=timeout) as response:
        payload = response.read().decode('utf-8')
        return response.status, json.loads(payload) if payload else {}


def p1_data_url(meter_url: str) -> str:
    meter_url = meter_url.rstrip('/')
    if meter_url.endswith('/api/v1/data'):
        return meter_url
    return f'{meter_url}/api/v1/data'


def check_p1_meter(meter_url: str):
    """Check direct P1 meter connectivity."""
    try:
        status_code, data = http_get_json(p1_data_url(meter_url))
        if status_code == 200:
            return True, f"✅ P1 Meter: {data.get('active_power_w', 0)}W"
        return False, f"❌ P1 Meter: HTTP {status_code}"
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return False, f"❌ P1 Meter: {exc}"


def check_web_dashboard(web_url: str):
    """Check whether the Flask dashboard is reachable."""
    try:
        with urlopen(web_url, timeout=5) as response:
            ok = response.status == 200
            if ok:
                return True, f"✅ Web Dashboard: Running at {web_url}"
            return False, f"❌ Web Dashboard: HTTP {response.status}"
    except (HTTPError, URLError, TimeoutError) as exc:
        return False, f"❌ Web Dashboard: {exc}"


def check_data_collection(db_path: str, stale_seconds: int):
    """Check that data exists and is still arriving recently."""
    if not os.path.exists(db_path):
        return False, f"❌ Data Collection: Database not found at {db_path}"

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        count, latest_ts, latest_iso = conn.execute(
            'SELECT COUNT(*), MAX(timestamp), MAX(datetime) FROM power_data'
        ).fetchone()
    except sqlite3.Error as exc:
        return False, f"❌ Data Collection: Database error ({exc})"
    finally:
        if conn is not None:
            conn.close()

    if not count:
        return False, "❌ Data Collection: No rows in power_data"

    if latest_ts is None:
        return False, "❌ Data Collection: No latest timestamp recorded"

    age_seconds = max(0.0, time.time() - float(latest_ts))
    stale = age_seconds > stale_seconds
    age_label = f"{age_seconds:.0f}s ago"
    prefix = "✅" if not stale else "⚠️ "
    state = "fresh" if not stale else "stale"
    return (
        not stale,
        f"{prefix} Data Collection: {count} rows, latest {latest_iso} ({age_label}, {state})",
    )


def check_monitoring_api(web_url: str, stale_seconds: int):
    """Ask the dashboard for collector health when the web app is running."""
    try:
        status_code, data = http_get_json(
            f"{web_url.rstrip('/')}/api/monitoring/status?stale_seconds={stale_seconds}"
        )
        if status_code != 200:
            return False, f"⚠️  Collector API: HTTP {status_code}"

        collector = data.get('collector', {})
        running = collector.get('running')
        stale = collector.get('stale')
        latest = collector.get('latest_power_datetime')
        seconds = collector.get('seconds_since_power_data')
        threads = data.get('threads', {})
        alive = sum(1 for info in threads.values() if info.get('alive'))
        return (
            bool(running) and not bool(stale),
            f"ℹ️  Collector API: running={running}, stale={stale}, latest={latest}, "
            f"age={seconds}, live_threads={alive}",
        )
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None, "ℹ️  Collector API: unavailable"


def parse_args():
    parser = argparse.ArgumentParser(description='Check HomeWizard runtime health.')
    parser.add_argument('--meter-url', default=DEFAULT_METER_URL)
    parser.add_argument('--web-url', default=DEFAULT_WEB_URL)
    parser.add_argument('--db-path', default=DEFAULT_DB_PATH)
    parser.add_argument('--stale-seconds', type=int, default=DEFAULT_STALE_SECONDS)
    return parser.parse_args()


def main():
    args = parse_args()

    print("🏠 HomeWizard P1 Monitor - System Status")
    print("=" * 50)
    print(f"Checked at: {datetime.now().isoformat(timespec='seconds')}")

    meter_ok, meter_msg = check_p1_meter(args.meter_url)
    web_ok, web_msg = check_web_dashboard(args.web_url)
    data_ok, data_msg = check_data_collection(args.db_path, args.stale_seconds)
    api_ok, api_msg = check_monitoring_api(args.web_url, args.stale_seconds)

    print(meter_msg)
    print(web_msg)
    print(data_msg)
    print(api_msg)

    print("\n📊 Quick Access:")
    print(f"   Dashboard: {args.web_url}")
    print(f"   Meter API:  {args.meter_url}")
    print(f"   Database:   {args.db_path}")

    print("\n💡 Commands:")
    print("   ./start_data_collector.sh  # Start the always-on collector")
    print("   ./start_web_ui.sh          # Start dashboard without collectors")
    print("   ./start_web_monitor.sh     # Start dashboard + collectors together")
    print("   ./p1_env/bin/python status.py")

    collector_ok = data_ok
    if collector_ok and web_ok:
        print("\n🟢 Collector and web UI look healthy.")
    elif collector_ok:
        print("\n🟢 Collector looks healthy. Direct HTTP checks may still be blocked or unavailable.")
    else:
        print("\n🟠 Collector needs attention - check the messages above.")


if __name__ == "__main__":
    main()
