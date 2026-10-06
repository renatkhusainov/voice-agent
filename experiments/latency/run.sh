#!/usr/bin/env bash
# One measured run: restart the app with LATENCY_LABEL=<label> plus any
# config overrides, drive 5 synthetic calls, stop the app.
#   experiments/latency/run.sh baseline
#   experiments/latency/run.sh prompt-cache LLM_PROMPT_CACHING=true
set -euo pipefail
label=$1; shift
port=8765
kill $(lsof -tiTCP:$port -sTCP:LISTEN) 2>/dev/null || true
sleep 1
env LATENCY_LABEL="$label" "$@" .venv/bin/uvicorn app.main:app --port $port > "app/temp/latency_server_$label.log" 2>&1 &
server=$!
for _ in $(seq 1 60); do curl -s "localhost:$port/health" >/dev/null && break; sleep 0.5; done
.venv/bin/python -m experiments.latency.drive_calls --label "$label" --port $port
kill $server
