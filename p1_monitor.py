#!/usr/bin/env python3
"""
HomeWizard P1 Meter Data Collector
Fetches, displays, and stores power meter data for analysis
"""

import json
import time
import requests
from datetime import datetime
import csv
import os
from typing import Dict, Any

class P1Monitor:
    def __init__(self, meter_url: str = "http://192.168.11.35", data_file: str = "p1_data.csv"):
        self.meter_url = meter_url
        self.data_file = data_file
        self.setup_data_file()
    
    def setup_data_file(self):
        """Initialize CSV file with headers if it doesn't exist"""
        if not os.path.exists(self.data_file):
            headers = [
                'timestamp', 'datetime', 'total_power_w', 'total_import_kwh', 'total_export_kwh',
                'power_l1_w', 'power_l2_w', 'power_l3_w',
                'voltage_l1_v', 'voltage_l2_v', 'voltage_l3_v', 
                'current_l1_a', 'current_l2_a', 'current_l3_a', 'current_total_a',
                'wifi_strength'
            ]
            with open(self.data_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(headers)
    
    def fetch_data(self) -> Dict[str, Any]:
        """Fetch current data from P1 meter"""
        try:
            response = requests.get(f"{self.meter_url}/api/v1/data", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"❌ Error fetching data: {e}")
            return None
    
    def store_data(self, data: Dict[str, Any]):
        """Store data to CSV file"""
        if not data:
            return
            
        timestamp = time.time()
        dt = datetime.now()
        
        row = [
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
        ]
        
        with open(self.data_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
    
    def display_data(self, data: Dict[str, Any]):
        """Display data in nice format"""
        if not data:
            return
            
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Clear screen and show header
        os.system('clear' if os.name == 'posix' else 'cls')
        
        print("=" * 80)
        print(f"🏠 HomeWizard P1 Meter Monitor - {now}")
        print("=" * 80)
        
        # Power consumption
        total_power = data.get('active_power_w', 0)
        print(f"\n⚡ POWER CONSUMPTION")
        print(f"   Total: {total_power:,}W ({total_power/1000:.2f} kW)")
        
        # Phase breakdown  
        l1 = data.get('active_power_l1_w', 0)
        l2 = data.get('active_power_l2_w', 0) 
        l3 = data.get('active_power_l3_w', 0)
        total_phases = l1 + l2 + l3
        
        print(f"\n📊 PHASE DISTRIBUTION")
        print(f"   Phase 1: {l1:4}W ({l1/total_phases*100:4.1f}%) {'▓'*int(l1/100)}")
        print(f"   Phase 2: {l2:4}W ({l2/total_phases*100:4.1f}%) {'▓'*int(l2/100)}")
        print(f"   Phase 3: {l3:4}W ({l3/total_phases*100:4.1f}%) {'▓'*int(l3/100)}")
        
        # Imbalance warning
        imbalance = max(l1,l2,l3) - min(l1,l2,l3)
        if imbalance > 1500:
            print(f"   ⚠️  Imbalance: {imbalance}W")
        
        # Electrical parameters
        print(f"\n🔌 ELECTRICAL PARAMETERS")
        print(f"   Voltage: L1={data.get('active_voltage_l1_v', 0):.1f}V  L2={data.get('active_voltage_l2_v', 0):.1f}V  L3={data.get('active_voltage_l3_v', 0):.1f}V")
        print(f"   Current: L1={data.get('active_current_l1_a', 0):.1f}A  L2={data.get('active_current_l2_a', 0):.1f}A  L3={data.get('active_current_l3_a', 0):.1f}A")
        print(f"   Total Current: {data.get('active_current_a', 0):.1f}A")
        
        # Utilization (assuming 35A main fuse)
        current_total = data.get('active_current_a', 0)
        util_35 = current_total / 35 * 100
        util_65 = current_total / 65 * 100
        print(f"   Main Fuse: {util_35:.1f}% (35A) | {util_65:.1f}% (65A)")
        
        # Energy totals
        print(f"\n📈 ENERGY TOTALS")
        print(f"   Imported: {data.get('total_power_import_kwh', 0)} kWh")
        print(f"   Exported: {data.get('total_power_export_kwh', 0)} kWh")
        
        # Cost estimation
        hourly_cost = total_power * 0.25 / 1000  # €0.25/kWh
        print(f"\n💰 COST ESTIMATE (€0.25/kWh)")
        print(f"   Current rate: €{hourly_cost:.2f}/hour")
        print(f"   If sustained: €{hourly_cost*24:.1f}/day")
        
        # Connection status
        wifi_strength = data.get('wifi_strength', 0)
        print(f"\n📡 CONNECTION")
        print(f"   WiFi: {data.get('wifi_ssid', 'Unknown')} ({wifi_strength}%)")
        
        print("\n" + "=" * 80)
        print("📁 Data stored to:", self.data_file)
        print("⏹️  Press Ctrl+C to stop monitoring")
        print("=" * 80)
    
    def run_continuous(self, interval: int = 60):
        """Run continuous monitoring"""
        print(f"🚀 Starting P1 meter monitoring (every {interval}s)...")
        
        try:
            while True:
                data = self.fetch_data()
                if data:
                    self.display_data(data)
                    self.store_data(data)
                else:
                    print("❌ Failed to fetch data, retrying in 30s...")
                    time.sleep(30)
                    continue
                    
                time.sleep(interval)
                
        except KeyboardInterrupt:
            print("\n\n👋 Monitoring stopped by user")
        except Exception as e:
            print(f"\n❌ Unexpected error: {e}")

def main():
    monitor = P1Monitor()
    
    # Test connection first
    print("🔍 Testing connection to P1 meter...")
    data = monitor.fetch_data()
    if data:
        print("✅ Connection successful!")
        monitor.display_data(data)
        monitor.store_data(data)
        print("\n🔄 Starting continuous monitoring in 3 seconds...")
        time.sleep(3)
        monitor.run_continuous(interval=60)  # Collect every minute
    else:
        print("❌ Cannot connect to P1 meter. Check IP address and network.")

if __name__ == "__main__":
    main()