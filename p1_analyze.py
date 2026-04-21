#!/usr/bin/env python3
"""
P1 Meter Data Analyzer
Analyzes collected power meter data and shows trends, statistics
"""

import pandas as pd
import sys
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import numpy as np

class P1Analyzer:
    def __init__(self, data_file: str = "p1_data.csv"):
        self.data_file = data_file
        self.df = None
        self.load_data()
    
    def load_data(self):
        """Load data from CSV file"""
        try:
            self.df = pd.read_csv(self.data_file)
            self.df['datetime'] = pd.to_datetime(self.df['datetime'])
            print(f"✅ Loaded {len(self.df)} data points from {self.data_file}")
        except FileNotFoundError:
            print(f"❌ Data file {self.data_file} not found. Run p1_monitor.py first.")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading data: {e}")
            sys.exit(1)
    
    def summary_stats(self):
        """Display summary statistics"""
        if self.df.empty:
            print("No data available")
            return
            
        print("=" * 60)
        print("📊 POWER CONSUMPTION SUMMARY")
        print("=" * 60)
        
        # Time range
        start_time = self.df['datetime'].min()
        end_time = self.df['datetime'].max()
        duration = end_time - start_time
        
        print(f"📅 Data Period: {start_time} to {end_time}")
        print(f"⏱️  Duration: {duration}")
        print(f"📈 Data Points: {len(self.df)}")
        
        # Power statistics
        power_col = 'total_power_w'
        print(f"\n⚡ POWER STATISTICS (W)")
        print(f"   Current: {self.df[power_col].iloc[-1]:,.0f}W")
        print(f"   Average: {self.df[power_col].mean():,.0f}W")
        print(f"   Minimum: {self.df[power_col].min():,.0f}W")
        print(f"   Maximum: {self.df[power_col].max():,.0f}W")
        print(f"   Std Dev: {self.df[power_col].std():,.0f}W")
        
        # Energy consumption
        if len(self.df) > 1:
            energy_diff = self.df['total_import_kwh'].iloc[-1] - self.df['total_import_kwh'].iloc[0]
            hours = duration.total_seconds() / 3600
            avg_power = energy_diff * 1000 / hours if hours > 0 else 0
            
            print(f"\n🔋 ENERGY CONSUMPTION")
            print(f"   Total Import: {self.df['total_import_kwh'].iloc[-1]:.3f} kWh")
            print(f"   Period Usage: {energy_diff:.3f} kWh")
            print(f"   Average Power: {avg_power:.0f}W (from energy)")
        
        # Phase balance analysis
        print(f"\n⚖️  PHASE BALANCE")
        l1_avg = self.df['power_l1_w'].mean()
        l2_avg = self.df['power_l2_w'].mean() 
        l3_avg = self.df['power_l3_w'].mean()
        total_avg = l1_avg + l2_avg + l3_avg
        
        print(f"   Phase 1: {l1_avg:.0f}W ({l1_avg/total_avg*100:.1f}%)")
        print(f"   Phase 2: {l2_avg:.0f}W ({l2_avg/total_avg*100:.1f}%)")
        print(f"   Phase 3: {l3_avg:.0f}W ({l3_avg/total_avg*100:.1f}%)")
        
        imbalance = max(l1_avg, l2_avg, l3_avg) - min(l1_avg, l2_avg, l3_avg)
        print(f"   Imbalance: {imbalance:.0f}W")
        
        # Cost estimates
        print(f"\n💰 COST ESTIMATES (€0.25/kWh)")
        current_hourly = self.df[power_col].iloc[-1] * 0.25 / 1000
        avg_hourly = self.df[power_col].mean() * 0.25 / 1000
        
        print(f"   Current rate: €{current_hourly:.2f}/hour")
        print(f"   Average rate: €{avg_hourly:.2f}/hour")
        print(f"   Daily (avg): €{avg_hourly * 24:.1f}")
        print(f"   Monthly (avg): €{avg_hourly * 24 * 30:.0f}")
    
    def recent_data(self, hours: int = 24):
        """Show recent data trends"""
        cutoff = datetime.now() - timedelta(hours=hours)
        recent = self.df[self.df['datetime'] >= cutoff]
        
        if recent.empty:
            print(f"No data from last {hours} hours")
            return
            
        print(f"\n📊 LAST {hours} HOURS TREND")
        print("=" * 60)
        
        # Power trend
        power_start = recent['total_power_w'].iloc[0]
        power_end = recent['total_power_w'].iloc[-1] 
        power_change = power_end - power_start
        
        print(f"Power: {power_start:.0f}W → {power_end:.0f}W ({power_change:+.0f}W)")
        
        # Show hourly averages for last 24h
        if len(recent) >= 5:
            recent_copy = recent.copy()
            recent_copy['hour'] = recent_copy['datetime'].dt.floor('H')
            hourly = recent_copy.groupby('hour')['total_power_w'].mean()
            
            print(f"\nHourly averages:")
            for hour, power in hourly.tail(12).items():
                hour_str = hour.strftime("%H:%M")
                bar = "▓" * int(power / 200)
                print(f"   {hour_str}: {power:4.0f}W {bar}")
    
    def plot_data(self, hours: int = 24):
        """Create plots of power consumption"""
        try:
            import matplotlib.pyplot as plt
            
            cutoff = datetime.now() - timedelta(hours=hours)
            recent = self.df[self.df['datetime'] >= cutoff]
            
            if recent.empty:
                print(f"No data from last {hours} hours to plot")
                return
                
            fig, axes = plt.subplots(2, 2, figsize=(15, 10))
            fig.suptitle(f'HomeWizard P1 Meter - Last {hours} Hours')
            
            # Total power
            axes[0,0].plot(recent['datetime'], recent['total_power_w'])
            axes[0,0].set_title('Total Power Consumption')
            axes[0,0].set_ylabel('Power (W)')
            axes[0,0].grid(True)
            
            # Phase distribution
            axes[0,1].plot(recent['datetime'], recent['power_l1_w'], label='Phase 1')
            axes[0,1].plot(recent['datetime'], recent['power_l2_w'], label='Phase 2') 
            axes[0,1].plot(recent['datetime'], recent['power_l3_w'], label='Phase 3')
            axes[0,1].set_title('Phase Distribution')
            axes[0,1].set_ylabel('Power (W)')
            axes[0,1].legend()
            axes[0,1].grid(True)
            
            # Voltage
            axes[1,0].plot(recent['datetime'], recent['voltage_l1_v'], label='L1')
            axes[1,0].plot(recent['datetime'], recent['voltage_l2_v'], label='L2')
            axes[1,0].plot(recent['datetime'], recent['voltage_l3_v'], label='L3')
            axes[1,0].set_title('Voltage')
            axes[1,0].set_ylabel('Voltage (V)')
            axes[1,0].legend()
            axes[1,0].grid(True)
            
            # Current
            axes[1,1].plot(recent['datetime'], recent['current_total_a'])
            axes[1,1].set_title('Total Current')
            axes[1,1].set_ylabel('Current (A)')
            axes[1,1].grid(True)
            
            plt.tight_layout()
            plt.xticks(rotation=45)
            
            plot_file = f"p1_plot_{hours}h.png"
            plt.savefig(plot_file, dpi=150, bbox_inches='tight')
            print(f"📈 Plot saved to {plot_file}")
            
        except ImportError:
            print("📊 Install matplotlib to generate plots: pip install matplotlib")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "plot":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        analyzer = P1Analyzer()
        analyzer.plot_data(hours)
        return
    
    analyzer = P1Analyzer()
    analyzer.summary_stats()
    analyzer.recent_data(24)
    
    print("\n" + "=" * 60)
    print("💡 Usage:")
    print("   python3 p1_analyze.py        # Show summary")
    print("   python3 p1_analyze.py plot   # Generate plots")
    print("   python3 p1_analyze.py plot 6 # Plot last 6 hours")

if __name__ == "__main__":
    main()