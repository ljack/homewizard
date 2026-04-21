#!/bin/bash
# Start HomeWizard P1 Web Monitor
cd "$(dirname "$0")"
source p1_env/bin/activate

echo "🚀 Starting HomeWizard P1 Web Monitor..."
echo "📊 Dashboard will be available at: http://localhost:5001"
echo "🌐 Or access from other devices: http://$(hostname -I | awk '{print $1}'):5001"
echo ""
echo "Press Ctrl+C to stop the server"
printf '=%.0s' {1..60}; echo

python3 web_monitor.py