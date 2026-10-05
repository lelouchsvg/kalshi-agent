#!/bin/bash
# Stops the Kalshi agent. Double-click "Stop Kalshi Agent.command" to run this.
cd "$(dirname "$0")"
ROOT="$(pwd)"
if [ -f data/agent.pids ]; then
  while read -r pid; do
    [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null
  done < data/agent.pids
  rm -f data/agent.pids
fi
sleep 1
# Make sure no agent process from this folder is left behind.
PATTERN="$ROOT/.venv/bin/python -m kalshi_agent"
pkill -TERM -f "$PATTERN" 2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$PATTERN" >/dev/null || break; sleep 1; done
pkill -KILL -f "$PATTERN" 2>/dev/null
echo "Kalshi agent stopped."
