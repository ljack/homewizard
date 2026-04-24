#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
source p1_env/bin/activate

PORT="${PORT:-5001}"
LAN_IP="$(
python3 - <<'PY'
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ip = ""
try:
    sock.connect(("8.8.8.8", 80))
    ip = sock.getsockname()[0]
except OSError:
    pass
finally:
    sock.close()

print(ip)
PY
)"

echo "🚀 Starting HomeWizard P1 Web Monitor..."
echo "📊 Dashboard will be available at: http://localhost:${PORT}"
if [ -n "${LAN_IP}" ]; then
  echo "🌐 Or access from other devices: http://${LAN_IP}:${PORT}"
fi
echo ""
echo "Press Ctrl+C to stop the server"
printf '=%.0s' {1..60}; echo

exec python3 web_monitor.py --port "${PORT}" "$@"
