#!/bin/bash
# Activate virtual environment and run P1 monitor
cd "$(dirname "$0")"
source p1_env/bin/activate
python3 p1_monitor.py