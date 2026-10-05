#!/usr/bin/env bash
# Consistent SQLite backup; keeps the newest 14.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
STAMP=$(date -u +%Y%m%d-%H%M%S)
.venv/bin/python -c "import sqlite3,sys; s=sqlite3.connect('data/kalshi_agent.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close()" "backups/kalshi_agent-$STAMP.db"
gzip "backups/kalshi_agent-$STAMP.db"
ls -1t backups/*.db.gz | tail -n +15 | xargs -r rm --
