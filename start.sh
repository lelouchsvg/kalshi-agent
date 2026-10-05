#!/bin/bash
# Starts the Kalshi agent on this Mac. Double-click "Start Kalshi Agent.command" to run this.
#
# Everything it downloads (a private copy of Python and the libraries the agent uses)
# lives inside this folder in .runtime/ and .venv/. Nothing is installed system-wide,
# and deleting this folder removes all of it.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
mkdir -p data logs .runtime

export UV_INSTALL_DIR="$ROOT/.runtime/bin"
export UV_PYTHON_INSTALL_DIR="$ROOT/.runtime/python"
export UV_CACHE_DIR="$ROOT/.runtime/cache"
export UV_NO_MODIFY_PATH=1
export PATH="$UV_INSTALL_DIR:$PATH"
UV="$UV_INSTALL_DIR/uv"
PY="$ROOT/.venv/bin/python"

say()  { printf '\n\033[1;33m%s\033[0m\n' "$*"; }
fail() {
  printf '\n\033[1;31m%s\033[0m\n' "$*"
  echo "---- last lines of logs/setup.log (send a screenshot of this to Claude) ----"
  tail -15 logs/setup.log 2>/dev/null
  [ -n "${AGENT_QUIET:-}" ] || read -r -p "Press Enter to close. " _
  exit 1
}

if [ -f data/agent.pids ] && kill -0 $(head -1 data/agent.pids) 2>/dev/null; then
  say "The agent is already running. Opening the dashboard."
  [ -n "${AGENT_QUIET:-}" ] || open "http://127.0.0.1:8080"
  exit 0
fi

# 1. Private Python toolchain (first run only, about 60 MB download)
if [ ! -x "$UV" ]; then
  say "First run: downloading a private copy of Python into this folder (one time only)..."
  curl -LsSf https://astral.sh/uv/install.sh -o .runtime/uv-install.sh >>logs/setup.log 2>&1 \
    || fail "Couldn't download the Python installer. Check your internet connection and try again."
  sh .runtime/uv-install.sh >>logs/setup.log 2>&1 || fail "The Python installer failed."
fi
if [ ! -x "$PY" ]; then
  "$UV" venv --python 3.12 .venv >>logs/setup.log 2>&1 || fail "Couldn't set up Python 3.12."
fi

# 2. Libraries (re-installed only when requirements change)
REQ_HASH=$(cat requirements*.txt | shasum | cut -d' ' -f1)
if [ "$(cat .runtime/req.hash 2>/dev/null)" != "$REQ_HASH" ]; then
  say "Installing the agent's libraries..."
  echo "=== $(date) installing libraries (macOS $(sw_vers -productVersion 2>/dev/null)) ===" >>logs/setup.log
  "$UV" pip install --python "$PY" -r requirements-dev.txt >>logs/setup.log 2>&1 \
    || fail "Couldn't install the libraries."
  # Optional extras: nice to have, never required.
  for pkg in psutil cryptography; do
    "$UV" pip install --python "$PY" --only-binary :all: "$pkg" >>logs/setup.log 2>&1 \
      || echo "Optional library $pkg not available on this Mac; continuing without it." | tee -a logs/setup.log
  done
  echo "$REQ_HASH" > .runtime/req.hash
fi

# 3. Safety: the full test suite must pass whenever the code has changed
CODE_HASH=$(find kalshi_agent tests config -type f \( -name '*.py' -o -name '*.sql' -o -name '*.yaml' -o -name '*.html' \) -print0 | sort -z | xargs -0 shasum | shasum | cut -d' ' -f1)
if [ "$(cat .runtime/tests.hash 2>/dev/null)" != "$CODE_HASH" ]; then
  say "Running safety tests..."
  if "$PY" -m pytest -q -p no:cacheprovider >logs/tests.log 2>&1; then
    tail -1 logs/tests.log
    echo "$CODE_HASH" > .runtime/tests.hash
  else
    tail -20 logs/tests.log
    fail "Tests failed, so the agent will NOT start. Tell Claude: 'tests failed on my Mac'."
  fi
fi

# 4. Start the collector and dashboard. Each restarts itself if it crashes, and they
#    keep running after this window is closed.
say "Starting the agent..."
nohup /bin/bash ./run_forever.sh collector kalshi_agent.service >/dev/null 2>&1 &
COLLECTOR=$!
nohup /bin/bash ./run_forever.sh dashboard kalshi_agent.dashboard.server >/dev/null 2>&1 &
DASHBOARD=$!
# Keep the Mac from idle-sleeping while the agent runs (the lid must stay open).
nohup caffeinate -i -w "$COLLECTOR" >/dev/null 2>&1 &
CAFFEINATE=$!
nohup /bin/bash ./update_loop.sh >/dev/null 2>&1 &
UPDATER=$!
printf '%s\n%s\n%s\n%s\n' "$COLLECTOR" "$DASHBOARD" "$CAFFEINATE" "$UPDATER" > data/agent.pids

for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -s -o /dev/null http://127.0.0.1:8080/ && break
  sleep 1
done
[ -n "${AGENT_QUIET:-}" ] || open "http://127.0.0.1:8080"

say "Kalshi agent is running in PAPER mode."
echo "Dashboard: http://127.0.0.1:8080  (only reachable from this Mac)"
echo "You can close this window; the agent keeps running."
echo "To stop it, double-click 'Stop Kalshi Agent.command'."
