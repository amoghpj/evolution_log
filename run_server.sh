#!/usr/bin/env bash
# Start the log server on this checkout's log. Run ./init_experiment.sh once first.
#
#   ./run_server.sh
#
# Everything it needs is in secrets/server.env, written by init_experiment.sh.
# Under systemd, make this script the ExecStart (see README.md).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "$ROOT/evolution_log.json" ] || { echo "STOP: no evolution_log.json -- run ./init_experiment.sh first" >&2; exit 1; }
[ -f "$ROOT/secrets/server.env" ] || { echo "STOP: no secrets/server.env -- run ./init_experiment.sh first" >&2; exit 1; }

set -a; . "$ROOT/secrets/server.env"; set +a
# Always THIS checkout, never a value remembered from somewhere else: an
# unset or stale LOG_REPO_PATH quietly serves, and accepts writes into,
# whatever log lives at that path.
export LOG_REPO_PATH="$ROOT"

cd "$ROOT/server"
echo "serving $(LOG_REPO_PATH="$ROOT" python3 -c 'import json,os;e=json.load(open(os.environ["LOG_REPO_PATH"]+"/evolution_log.json")).get("experiment",{});print(e.get("name"),"--",e.get("title"))' 2>/dev/null || echo "$ROOT") on $BIND_HOST:$PORT -- viewer at http://localhost:$PORT/viewer/"
exec "$UVICORN" app.main:app --host "$BIND_HOST" --port "$PORT"
