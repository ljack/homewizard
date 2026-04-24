#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
source p1_env/bin/activate

PORT="${PORT:-5001}"

echo "🚀 Starting HomeWizard web UI..."
echo "📊 Dashboard will be available at: http://localhost:${PORT}"
echo "📡 Web-only mode expects the collector service to be running separately."
echo ""

exec python3 web_monitor.py --web-only --port "${PORT}" "$@"
