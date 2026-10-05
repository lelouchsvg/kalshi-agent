#!/bin/bash
# Keeps one part of the agent running: restarts it 10 seconds after any crash.
# Usage: run_forever.sh <name> <python module>   (start.sh calls this; you don't need to)
cd "$(dirname "$0")"
NAME="$1"; MODULE="$2"; PY="$(pwd)/.venv/bin/python"
child=""
trap '[ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM INT HUP
while true; do
  "$PY" -m "$MODULE" >>"logs/$NAME.out" 2>&1 &
  child=$!
  wait "$child"
  echo "$(date) $NAME exited; restarting in 10s" >>"logs/$NAME.out"
  sleep 10 & wait $!
done
