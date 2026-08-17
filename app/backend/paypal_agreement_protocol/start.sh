#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
exec python3 web.py --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}"
