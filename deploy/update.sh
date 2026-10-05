#!/usr/bin/env bash
# Pull new code from GitHub. Keep it only if every test passes; otherwise roll back.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch --quiet origin main
OLD=$(git rev-parse HEAD); NEW=$(git rev-parse origin/main)
[ "$OLD" = "$NEW" ] && exit 0
git merge --ff-only --quiet origin/main
.venv/bin/pip install --quiet -r requirements-dev.txt
if .venv/bin/python -m pytest -q; then
  echo "Updated $OLD -> $NEW (tests passed)"
  sudo /bin/systemctl restart kalshi-collector kalshi-dashboard
else
  echo "Tests FAILED on $NEW; rolling back to $OLD" >&2
  git reset --hard --quiet "$OLD"
  .venv/bin/pip install --quiet -r requirements-dev.txt
  exit 1
fi
