#!/usr/bin/env python3
"""
Initialize chart markers table in existing P1 database
"""

import sqlite3
import os

def init_markers_table():
    """Initialize markers table if it doesn't exist"""
    if not os.path.exists('p1_data.db'):
        print("❌ Database file p1_data.db not found")
        print("Run the web monitor first to create the database")
        return False
    
    try:
        conn = sqlite3.connect('p1_data.db')
        cursor = conn.cursor()
        
        # Check if markers table exists
        cursor.execute('''
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='chart_markers'
        ''')
        
        if cursor.fetchone():
            print("✅ Markers table already exists")
        else:
            print("🔧 Creating chart markers table...")
            cursor.execute('''
                CREATE TABLE chart_markers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    datetime TEXT,
                    label TEXT,
                    description TEXT,
                    color TEXT DEFAULT '#ff6b6b',
                    created_at REAL
                )
            ''')
            
            # Add a sample marker for demonstration
            import time
            from datetime import datetime
            
            sample_time = time.time() - 3600  # 1 hour ago
            sample_dt = datetime.fromtimestamp(sample_time)
            
            cursor.execute('''
                INSERT INTO chart_markers (timestamp, datetime, label, description, color, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                sample_time,
                sample_dt.isoformat(),
                "System Started",
                "P1 monitoring system initialization",
                "#2ecc71",
                time.time()
            ))
            
            conn.commit()
            print("✅ Chart markers table created successfully")
            print("📍 Added sample marker")
        
        # Show current markers
        cursor.execute('SELECT COUNT(*) FROM chart_markers')
        count = cursor.fetchone()[0]
        print(f"📊 Current markers: {count}")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Error initializing markers table: {e}")
        return False

if __name__ == "__main__":
    print("🏠 HomeWizard P1 Monitor - Markers Database Initialization")
    print("=" * 60)
    
    if init_markers_table():
        print("\n🎉 Database initialization complete!")
        print("You can now use the chart markers feature.")
    else:
        print("\n❌ Database initialization failed.")
        print("Please check the error messages above.")