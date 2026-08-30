#!/usr/bin/env bash
# Start the Sony FCB camera web GUI.
cd "$(dirname "$0")"
exec python3 app.py --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}" "$@"
