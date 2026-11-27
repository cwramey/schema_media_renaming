#!/usr/bin/env bash
# Start the Schema backend (demo mode) and the Vite dev server together. Ctrl+C stops both.
#
#   scripts/dev.sh              # demo mode on :8420, Vite on :5173
#   scripts/dev.sh --no-demo    # use your real data dir (backend/data)
#   PORT=8421 scripts/dev.sh
#
# Works on Linux, macOS and Git Bash on Windows.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
PORT="${PORT:-8420}"
DEMO=1
[ "${1:-}" = "--no-demo" ] && DEMO=0

PY="$BACKEND/.venv/bin/python"
[ -x "$PY" ] || PY="$BACKEND/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
    echo "No virtualenv at backend/.venv. Create it with:" >&2
    echo "  cd backend && python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
    exit 1
fi
command -v npm >/dev/null 2>&1 || { echo "npm not found; install Node.js 20 or newer" >&2; exit 1; }
[ -d "$FRONTEND/node_modules" ] || (cd "$FRONTEND" && npm ci)

export SCHEMA_DEMO="$DEMO" SCHEMA_HOST=127.0.0.1 SCHEMA_PORT="$PORT" SCHEMA_BACKEND="http://127.0.0.1:$PORT"

echo "Schema backend  -> http://127.0.0.1:$PORT  (demo: $DEMO)"
echo "Vite dev server -> http://127.0.0.1:5173"
(cd "$BACKEND" && exec "$PY" -m schema_app) &
BACKEND_PID=$!

cleanup() {
    trap - EXIT INT TERM
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
    echo "Stopped."
}
trap cleanup EXIT INT TERM

sleep 0.5
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "The backend exited immediately; see the output above." >&2
    exit 1
fi
cd "$FRONTEND"
npm run dev
