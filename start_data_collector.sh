#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
source scripts/load_env.sh
load_project_env .env
source p1_env/bin/activate

echo "🚀 Starting HomeWizard data collector..."
echo "🔁 Collector-only mode keeps writing fresh data even if the web UI is stopped."
echo ""

exec python3 web_monitor.py --collector-only "$@"
