#!/bin/bash
# Installs this copy of the agent over your existing one WITHOUT touching collected data.
#
#   In Terminal:  bash   (then drag this file in, press Enter)
#
# Keeps: data/ (your database), logs/, .env (keys, if any), and the private Python.
# Backs up the database and your settings file first, then restarts the agent.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$HOME/kalshi-agent}"

say()  { printf '\n\033[1;33m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*"; read -r -p "Press Enter to close. " _; exit 1; }

if [ "$SRC" = "$(cd "$TARGET" 2>/dev/null && pwd)" ]; then
  say "This is already your agent folder; restarting it with the new code."
  bash "$TARGET/stop.sh" >/dev/null 2>&1 || true
  exec bash "$TARGET/start.sh"
fi
[ -f "$TARGET/start.sh" ] || fail "Couldn't find your agent at $TARGET. Tell Claude where the kalshi-agent folder is."

say "Stopping the running agent..."
bash "$TARGET/stop.sh" >/dev/null 2>&1 || true

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$TARGET/data/backups"
if [ -f "$TARGET/data/kalshi_agent.db" ]; then
  say "Backing up your collected data..."
  for f in "$TARGET"/data/kalshi_agent.db "$TARGET"/data/kalshi_agent.db-wal; do
    if [ -f "$f" ]; then cp "$f" "$TARGET/data/backups/$(basename "$f").$STAMP"; fi
  done
  # keep only the 3 newest backups
  { ls -1t "$TARGET"/data/backups/kalshi_agent.db.* 2>/dev/null || true; } | tail -n +4 | while read -r old; do
    rm -f "$old" "${old/kalshi_agent.db./kalshi_agent.db-wal.}"; done
fi
if [ -f "$TARGET/config/settings.yaml" ]; then
  cp "$TARGET/config/settings.yaml" "$TARGET/data/backups/settings.yaml.$STAMP"
  { ls -1t "$TARGET"/data/backups/settings.yaml.* 2>/dev/null || true; } | tail -n +4 | while read -r old; do
    rm -f "$old"; done
fi

say "Copying the new code..."
for item in "$SRC"/* "$SRC"/.[!.]*; do
  [ -e "$item" ] || continue
  case "$(basename "$item")" in
    data|logs|.runtime|.venv|.env|.git) continue ;;
  esac
  cp -Rp "$item" "$TARGET/" || fail "Copying failed. Your data is untouched."
done

say "Starting the updated agent (it runs its safety tests first)..."
exec bash "$TARGET/start.sh"
