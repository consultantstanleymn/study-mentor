#!/bin/bash
# Start the Study Mentor server on http://127.0.0.1:8765
cd "$(dirname "$0")/server" || exit 1
exec "$(pwd)/../venv/bin/python" -m uvicorn app:app --host 127.0.0.1 --port 8765 --log-level warning
