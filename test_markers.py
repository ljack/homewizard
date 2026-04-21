#!/usr/bin/env python3
"""
Test script to demonstrate chart markers functionality
"""

import requests
import time
import json
from datetime import datetime, timedelta

def test_markers():
    base_url = "http://localhost:5001"
    
    print("🧪 Testing Chart Markers API")
    print("=" * 50)
    
    # Get current time for marker
    current_time = time.time()
    one_hour_ago = current_time - 3600
    
    # Test 1: Add a marker
    print("1. Adding test marker...")
    marker_data = {
        "timestamp": one_hour_ago,
        "label": "Pool heater turned off",
        "description": "Turned off pool heater to reduce power consumption during peak hours",
        "color": "#e74c3c"
    }
    
    response = requests.post(f"{base_url}/api/markers", json=marker_data)
    if response.status_code == 200:
        marker = response.json()
        print(f"   ✅ Marker added with ID: {marker['id']}")
        print(f"   📍 Label: {marker['label']}")
        print(f"   🕒 Time: {marker['datetime']}")
        marker_id = marker['id']
    else:
        print(f"   ❌ Failed to add marker: {response.text}")
        return
    
    # Test 2: Get all markers
    print("\n2. Fetching all markers...")
    response = requests.get(f"{base_url}/api/markers")
    if response.status_code == 200:
        markers = response.json()
        print(f"   ✅ Found {len(markers)} markers:")
        for i, marker in enumerate(markers, 1):
            dt = datetime.fromisoformat(marker['datetime'])
            print(f"   {i}. {marker['label']} - {dt.strftime('%H:%M:%S')} ({marker['color']})")
    else:
        print(f"   ❌ Failed to fetch markers: {response.text}")
        return
    
    # Test 3: Get markers for time range
    print("\n3. Testing time range query...")
    start_time = datetime.now() - timedelta(hours=25)
    end_time = datetime.now()
    
    params = {
        "start": start_time.isoformat(),
        "end": end_time.isoformat()
    }
    
    response = requests.get(f"{base_url}/api/markers", params=params)
    if response.status_code == 200:
        range_markers = response.json()
        print(f"   ✅ Found {len(range_markers)} markers in last 25 hours")
    else:
        print(f"   ❌ Failed to query time range: {response.text}")
    
    # Test 4: Test chart view (just check if it loads)
    print("\n4. Testing full-screen chart...")
    response = requests.get(f"{base_url}/chart")
    if response.status_code == 200:
        print(f"   ✅ Chart page loads successfully")
        print(f"   🔗 View at: {base_url}/chart")
    else:
        print(f"   ❌ Chart page failed to load: {response.status_code}")
    
    # Test 5: Clean up - delete test marker
    print("\n5. Cleaning up test marker...")
    if 'marker_id' in locals():
        response = requests.delete(f"{base_url}/api/markers/{marker_id}")
        if response.status_code == 200:
            print(f"   ✅ Test marker deleted")
        else:
            print(f"   ⚠️ Warning: Could not delete test marker")
    
    print("\n" + "=" * 50)
    print("🎉 Chart Markers API Test Complete!")
    print("\nHow to use:")
    print("1. Open the dashboard: http://localhost:5001")
    print("2. Click the expand button on the power chart")
    print("3. In full-screen view, click any point on the chart")
    print("4. Add a label like 'Heat pump maintenance'")
    print("5. Save the marker - it will appear on future chart views")

if __name__ == "__main__":
    test_markers()