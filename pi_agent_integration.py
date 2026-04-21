#!/usr/bin/env python3
"""
Pi Agent Integration for P1 Monitor Chat
Routes chat messages to pi command if available
"""

import subprocess
import json
import os
import tempfile

class PiAgentChat:
    def __init__(self):
        self.pi_available = self.check_pi_availability()
    
    def check_pi_availability(self):
        """Check if pi command is available"""
        try:
            result = subprocess.run(['pi', '--version'], 
                                  capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False
    
    def chat_with_pi(self, message, context_data=None):
        """Send message to pi agent with P1 meter context"""
        if not self.pi_available:
            return self.fallback_response(message, context_data)
        
        try:
            # Prepare context for pi agent
            context = self.prepare_context(context_data)
            full_prompt = f"""
{context}

User Question: {message}

Please provide a helpful analysis of the power consumption data, focusing on:
- Energy efficiency insights
- Cost implications  
- Safety considerations
- Practical recommendations

Be conversational and specific to the user's Finnish home setup with heat pumps and electric heating.
"""
            
            # Create temporary file for prompt
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                f.write(full_prompt)
                prompt_file = f.name
            
            try:
                # Call pi agent
                result = subprocess.run([
                    'pi', 'chat', '--file', prompt_file
                ], capture_output=True, text=True, timeout=30)
                
                if result.returncode == 0:
                    return result.stdout.strip()
                else:
                    return self.fallback_response(message, context_data)
                    
            finally:
                # Clean up temporary file
                os.unlink(prompt_file)
                
        except Exception as e:
            print(f"Error calling pi agent: {e}")
            return self.fallback_response(message, context_data)
    
    def _get_value(self, latest, *keys, default=0):
        """Read the first available key from live API or DB-shaped dicts."""
        if isinstance(latest, dict):
            for key in keys:
                value = latest.get(key)
                if value is not None:
                    return value
        return default

    def _get_price_context(self, latest):
        fallback_price = 0.25

        if isinstance(latest, dict):
            spot = latest.get('spot_price')
            if isinstance(spot, dict) and spot.get('available') and spot.get('price_eur_per_kwh') is not None:
                price = float(spot['price_eur_per_kwh'])
                cents = float(spot.get('price_cents_per_kwh', price * 100))
                return price, f"FI spot {cents:.2f} c/kWh"

            direct_price = latest.get('price_eur_per_kwh')
            if direct_price is not None:
                price = float(direct_price)
                return price, f"{price * 100:.2f} c/kWh"

        return fallback_price, "fallback 25.00 c/kWh"

    def prepare_context(self, data):
        """Prepare context string from P1 meter data."""
        if not data:
            return "No current P1 meter data available."

        latest = data[-1] if isinstance(data, list) else data

        power = self._get_value(latest, 'active_power_w', 'total_power_w', default=0)
        l1 = self._get_value(latest, 'active_power_l1_w', 'power_l1_w', default=0)
        l2 = self._get_value(latest, 'active_power_l2_w', 'power_l2_w', default=0)
        l3 = self._get_value(latest, 'active_power_l3_w', 'power_l3_w', default=0)
        v1 = self._get_value(latest, 'active_voltage_l1_v', 'voltage_l1_v', default=0)
        v2 = self._get_value(latest, 'active_voltage_l2_v', 'voltage_l2_v', default=0)
        v3 = self._get_value(latest, 'active_voltage_l3_v', 'voltage_l3_v', default=0)
        c1 = self._get_value(latest, 'active_current_l1_a', 'current_l1_a', default=0)
        c2 = self._get_value(latest, 'active_current_l2_a', 'current_l2_a', default=0)
        c3 = self._get_value(latest, 'active_current_l3_a', 'current_l3_a', default=0)
        current_total = self._get_value(latest, 'active_current_a', 'current_total_a', default=0)
        total_import = self._get_value(latest, 'total_power_import_kwh', 'total_import_kwh', default=0)
        wifi_strength = self._get_value(latest, 'wifi_strength', default=0)
        price_eur_per_kwh, price_label = self._get_price_context(latest)

        context = f"""
HomeWizard P1 Meter Current Reading:

Power Consumption:
- Total: {power}W ({power/1000:.1f} kW)
- Phase 1: {l1}W
- Phase 2: {l2}W
- Phase 3: {l3}W

Electrical Parameters:
- Voltage: L1={v1:.1f}V, L2={v2:.1f}V, L3={v3:.1f}V
- Current: L1={c1:.1f}A, L2={c2:.1f}A, L3={c3:.1f}A
- Total Current: {current_total:.1f}A

Energy:
- Total Import: {total_import} kWh
- WiFi Signal: {wifi_strength}%
- Electricity Price: {price_label} ({price_eur_per_kwh:.5f} €/kWh)

Home Setup Context:
- Finnish household with 2 ILP heat pumps
- Electric floor heating (shower + eteinen)
- Fridge + 2 freezers
- Main fuse: 35A or 65A (TBD)

Key Concerns:
- Phase imbalance: Phase 2 severely underloaded vs Phase 3
- High winter heating loads normal for Finnish climate
- Heat pumps provide efficient heating (COP 2.5-3.0)
"""
        return context

    def fallback_response(self, message, context_data):
        """Provide fallback analysis when pi agent unavailable."""
        message_lower = message.lower()

        if not context_data:
            return "I don't have current meter data to analyze. Please ensure monitoring is active."

        latest = context_data[-1] if isinstance(context_data, list) else context_data

        if isinstance(latest, dict):
            power = self._get_value(latest, 'active_power_w', 'total_power_w', default=0)
            l1 = self._get_value(latest, 'active_power_l1_w', 'power_l1_w', default=0)
            l2 = self._get_value(latest, 'active_power_l2_w', 'power_l2_w', default=0)
            l3 = self._get_value(latest, 'active_power_l3_w', 'power_l3_w', default=0)
        else:
            power = latest[2]
            l1 = latest[5]
            l2 = latest[6]
            l3 = latest[7]

        total_phase_power = max(l1 + l2 + l3, 1)
        price_eur_per_kwh, price_label = self._get_price_context(latest)

        if any(word in message_lower for word in ['cost', 'expensive', 'money', 'price', 'hinta', 'maksa', 'kallis', 'euro']):
            hourly = power * price_eur_per_kwh / 1000
            daily = hourly * 24
            monthly = daily * 30
            return f"""💰 **Cost Analysis:**

Current consumption: {power:,}W
Price basis: {price_label}
- Hourly cost: €{hourly:.2f}
- If sustained 24h: €{daily:.1f}/day
- Monthly projection: €{monthly:.0f}/month

**Good news:** Your heat pumps are efficient! They're delivering 2-3x the heat energy vs direct electric heating. This consumption is normal for Finnish winter heating.

**Tip:** Heat pumps cycle on/off, so your actual average will be lower than current peak usage."""

        elif any(word in message_lower for word in ['phase', 'balance', 'imbalance', 'vaihe', 'tasapaino']):
            imbalance = max(l1, l2, l3) - min(l1, l2, l3)
            phases = [l1, l2, l3]
            max_idx = phases.index(max(phases))
            min_idx = phases.index(min(phases))

            return f"""⚖️ **Phase Balance Analysis:**

Current distribution:
- Phase 1: {l1:,}W ({l1/total_phase_power*100:.1f}%)
- Phase 2: {l2:,}W ({l2/total_phase_power*100:.1f}%)
- Phase 3: {l3:,}W ({l3/total_phase_power*100:.1f}%)

**Imbalance:** {imbalance:,}W difference

{'✅ **Good balance!**' if imbalance < 1000 else f'⚠️ **Significant imbalance!** Phase {max_idx+1} is overloaded while Phase {min_idx+1} is underused.'}

**Recommendation:** Move some appliances (like floor heating or freezers) from Phase {max_idx+1} to Phase {min_idx+1} to better distribute the load."""

        elif any(word in message_lower for word in ['normal', 'typical', 'average', 'normaali', 'tyypillinen', 'keskimäär']):
            return f"""📊 **Consumption Assessment:**

Your current {power:,}W ({power/1000:.1f}kW) is **normal** for:
✅ Finnish home with 2 heat pumps
✅ Electric floor heating
✅ Multiple cooling appliances
✅ Winter heating loads

**Context:**
- Typical Finnish home: 15,000-20,000 kWh/year
- With heat pumps: Very efficient vs direct electric heating
- Winter peaks normal: Heat pumps work harder in cold weather

**Your setup is actually quite efficient!** Heat pumps provide excellent value."""

        elif any(word in message_lower for word in ['heat pump', 'heating', 'lämm', 'pumppu']):
            heat_pump_power = max(power - 600 - 1500, 0)
            return f"""🔥 **Heat Pump Analysis:**

Estimated heat pump consumption: ~{heat_pump_power:,}W
- This likely delivers {heat_pump_power*2.5/1000:.1f}-{heat_pump_power*3/1000:.1f}kW of heat
- COP (efficiency): 2.5-3.0 (excellent for winter)
- Much better than direct electric heating!

**Why this consumption is good:**
- Heat pumps extract heat from outside air
- Even at 0°C, they're 2-3x more efficient than resistive heating
- Your floor heating supplements efficiently in wet areas

**Normal operation:** Heat pumps cycle based on indoor temperature and outside conditions."""

        elif any(word in message_lower for word in ['efficiency', 'save', 'efficient', 'sääst', 'tehok']):
            return f"""⚡ **Efficiency Insights:**

**What you're doing right:**
✅ Heat pumps instead of direct electric heating
✅ Floor heating only in key areas (shower, eteinen)
✅ Good insulation (moderate consumption for large heating load)

**Potential improvements:**
🔧 Fix phase imbalance (move appliances between phases)
🏠 Consider smart thermostats for heat pumps
💡 Monitor patterns to identify optimization opportunities

**Current efficiency:** Very good! Your heat pumps are delivering excellent value."""

        else:
            return f"""🤖 **Power Analysis:**

Current reading: {power:,}W ({power/1000:.1f}kW)

**Key insights:**
- Consumption normal for Finnish winter heating
- Heat pump efficiency: Excellent (COP ~2.5-3.0)
- Phase balance: {'Good' if max(l1, l2, l3) - min(l1, l2, l3) < 1000 else 'Needs attention'}
- Cost impact: ~€{power * 24 * price_eur_per_kwh / 1000:.1f}/day if sustained ({price_label})

**Ask me about:**
- Specific costs and savings
- Phase balance recommendations
- Heat pump efficiency
- Typical consumption patterns
- Energy optimization tips"""

# Test function
if __name__ == "__main__":
    agent = PiAgentChat()
    print(f"Pi agent available: {agent.pi_available}")
    
    test_data = {
        'total_power_w': 5200,
        'power_l1_w': 2100,
        'power_l2_w': 400,
        'power_l3_w': 2700,
        'current_total_a': 22.5
    }
    
    response = agent.chat_with_pi("How much is this costing me?", test_data)
    print(f"\nTest response:\n{response}")