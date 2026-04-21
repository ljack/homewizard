#!/bin/bash
# Activate virtual environment and run P1 analyzer
cd "$(dirname "$0")"
source p1_env/bin/activate
python3 p1_analyze.py "$@"