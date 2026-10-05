#!/bin/bash
# Installs this copy of the agent over your existing one WITHOUT touching collected data.
#
#   In Terminal:  bash   (then drag this file in, press Enter)
#
# Keeps: data/ (your database), logs/, .env (keys, if any), and the private Python.
# Backs up the database and your settings file first, then restarts the agent.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  # Find the copy that is already running/collecting: it has a data/kalshi_agent.db.
  # First ask macOS where the running agent lives, then look in the usual places.
  RUNNING=""
  if command -v lsof >/dev/null 2>&1; then
    for pid in $(pgrep -f run_forever.sh 2>/dev/null || true); do
      d="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
      case "$d" in *"/.Trash/"*|"") ;; *) RUNNING="$d"; break ;; esac
    done
  fi
  CANDS=("$HOME/kalshi-agent" "$HOME/Desktop/kalshi-agent" "$HOME/Documents/kalshi-agent" "$HOME"/Downloads/kalshi-agent*)
  if [ -n "$RUNNING" ]; then CANDS=("$RUNNING" "${CANDS[@]}"); fi
  for cand in "${CANDS[@]}"; do
    c="$(cd "$cand" 2>/dev/null && pwd)" || continue
    if [ "$c" != "$SRC" ] && [ -f "$c/data/kalshi_agent.db" ]; then TARGET="$c"; break; fi
  done
  TARGET="${TARGET:-$HOME/kalshi-agent}"
fi
echo "Updating the agent in: $TARGET"

say()  { printf '\n\033[1;33m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*"; read -r -p "Press Enter to close. " _; exit 1; }

if [ "$SRC" = "$(cd "$TARGET" 2>/dev/null && pwd)" ]; then
  say "This is already your agent folder; restarting it with the new code."
  bash "$TARGET/stop.sh" >/dev/null 2>&1 || true
  exec bash "$TARGET/start.sh"
fi
if [ ! -f "$TARGET/start.sh" ]; then
  if [ -n "${1:-}" ]; then
    fail "Couldn't find your agent at $TARGET. Drag in the old kalshi-agent folder (the one with a data folder inside)."
  fi
  # No existing install anywhere: make this copy the agent, in the home folder.
  # (A ~/kalshi-agent holding only secrets/ or .env is fine: those are kept.)
  TARGET="$HOME/kalshi-agent"
  say "No existing agent found. Installing a fresh copy at $TARGET..."
  pkill -f run_forever.sh 2>/dev/null || true
  pkill -f "kalshi_agent" 2>/dev/null || true
  mkdir -p "$TARGET"
  for item in "$SRC"/* "$SRC"/.[!.]*; do
    [ -e "$item" ] || continue
    case "$(basename "$item")" in .env|secrets|data|logs) continue ;; esac
    cp -Rp "$item" "$TARGET/" || fail "Copying failed."
  done
  exec bash "$TARGET/start.sh"
fi

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

# A key saved by hand into ~/kalshi-agent while the agent lives elsewhere: bring it along.
HOMEDIR="$HOME/kalshi-agent"
if [ "$TARGET" != "$HOMEDIR" ] && [ ! -f "$TARGET/.env" ] && [ -s "$HOMEDIR/secrets/kalshi.key" ] && [ -f "$HOMEDIR/.env" ]; then
  say "Moving your Kalshi API key into the agent folder..."
  mkdir -p "$TARGET/secrets" && chmod 700 "$TARGET/secrets"
  cp -p "$HOMEDIR/secrets/kalshi.key" "$TARGET/secrets/kalshi.key" && chmod 600 "$TARGET/secrets/kalshi.key"
  grep -v '^KALSHI_PRIVATE_KEY_PATH=' "$HOMEDIR/.env" > "$TARGET/.env" || true
  echo "KALSHI_PRIVATE_KEY_PATH=$TARGET/secrets/kalshi.key" >> "$TARGET/.env"
  chmod 600 "$TARGET/.env"
fi

say "Starting the updated agent (it runs its safety tests first)..."
exec bash "$TARGET/start.sh"
