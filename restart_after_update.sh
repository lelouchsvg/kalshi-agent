#!/bin/bash
# Run by the updater after it installs a new version. If the new version won't start,
# it puts the previous version back and starts that instead.
cd "$(dirname "$0")"
sleep 2
echo "$(date) restarting with the new version"
bash stop.sh
if ! AGENT_QUIET=1 bash start.sh </dev/null; then
  echo "$(date) new version failed to start; restoring the previous version"
  cp -Rp data/backups/code_prev/. ./
  rm -f .runtime/tests.hash
  AGENT_QUIET=1 bash start.sh </dev/null
fi
