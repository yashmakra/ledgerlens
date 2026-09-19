#!/bin/sh
set -eu
python -m app.worker &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' TERM INT EXIT
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
