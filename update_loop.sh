#!/bin/bash
# Checks for agent updates in the background (start.sh runs this; you don't need to).
# Every check runs the full safety tests on the new version before installing it.
cd "$(dirname "$0")"
PY="$(pwd)/.venv/bin/python"
MIN=$("$PY" -c 'from kalshi_agent.config import load_settings; print(load_settings().update_interval_min)' 2>/dev/null || echo 30)
child=""
trap '[ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM INT HUP
sleep 120 & child=$!; wait $child
while true; do
  rm -f data/update_now
  "$PY" -m kalshi_agent.updater >>logs/update.log 2>&1 & child=$!; wait $child
  # wait MIN minutes, or less if "Check now" is pressed on the dashboard
  for _ in $(seq 1 "$MIN"); do
    [ -f data/update_now ] && break
    sleep 60 & child=$!; wait $child
  done
done
