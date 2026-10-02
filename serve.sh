#!/usr/bin/env bash
# Serve the evolution log + viewer over http so viewer.html can fetch the JSON.
# Browsers block fetch() on file:// URLs, so double-clicking viewer.html will not work.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8777}"
URL="http://localhost:${PORT}/viewer.html"

echo "Serving $(pwd)"
echo "  -> ${URL}"
echo "  (Ctrl-C to stop)"

# Open the browser once the server is up, without blocking the server itself.
( sleep 1; command -v open >/dev/null && open "${URL}" || true ) &

exec python3 -m http.server "${PORT}" --bind 0.0.0.0
