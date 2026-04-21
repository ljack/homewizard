#!/usr/bin/env python3
"""
Quick status check for P1 Monitor system
"""

import requests
import os
import sqlite3
from datetime import datetime

def check_p1_meter():
    """Check P1 meter connectivity"""
    try:
        response = requests.get("http://192.168.11.35/api/v1/data", timeout=5)
        if response.status_code == 200:
            data = response.json()
            return True, f"✅ P1 Meter: {data.get('active_power_w', 0)}W"
        else:
            return False, "❌ P1 Meter: HTTP Error"
    except:
        return False, "❌ P1 Meter: Connection Failed"

def check_web_dashboard():
    """Check web dashboard"""
    try:
        response = requests.get("http://localhost:5001", timeout=5)
        return response.status_code == 200, "✅ Web Dashboard: Running on port 5001" if response.status_code == 200 else "❌ Web Dashboard: Not responding"
    except:
        return False, "❌ Web Dashboard: Not running"

def check_data_collection():
    """Check if data is being collected"""
    try:
        if os.path.exists('p1_data.db'):
            conn = sqlite3.connect('p1_data.db')
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*), MAX(datetime) FROM power_data')
            count, latest = cursor.fetchone()
            conn.close()
            
            if count and count > 0:
                return True, f"✅ Data Collection: {count} records, latest: {latest}"
            else:
                return False, "❌ Data Collection: No data in database"
        else:
            return False, "❌ Data Collection: Database not found"
    except:
        return False, "❌ Data Collection: Database error"

def main():
    print("🏠 HomeWizard P1 Monitor - System Status")
    print("=" * 50)
    
    meter_ok, meter_msg = check_p1_meter()
    web_ok, web_msg = check_web_dashboard()
    data_ok, data_msg = check_data_collection()
    
    print(meter_msg)
    print(web_msg)
    print(data_msg)
    
    print("\n📊 Quick Access:")
    if web_ok:
        print("   Dashboard: http://localhost:5001")
        print("   Mobile:    http://192.168.5.247:5001")
    
    print("\n💡 Commands:")
    print("   ./start_web_monitor.sh  # Start web dashboard")
    print("   ./run_analyze.sh        # Command line analysis")
    print("   python3 status.py       # Check system status")
    
    all_ok = meter_ok and web_ok and data_ok
    print(f"\n{'🟢 All systems operational!' if all_ok else '🟡 Some issues detected - check above'}")

if __name__ == "__main__":
    main()