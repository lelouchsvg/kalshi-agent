#!/usr/bin/env bash
# One-time server setup for a fresh Ubuntu 24.04 VPS. Run as root:
#   bash setup_vps.sh git@github.com:lelouchsvg/kalshi-agent.git
# It installs everything, runs the tests, and starts the agent in PAPER mode.
# Safe to run again; it skips steps that are already done.
set -euo pipefail
REPO="${1:?usage: bash setup_vps.sh <github-ssh-url>}"
APP_USER=kalshi
HOME_DIR=/home/$APP_USER
APP=$HOME_DIR/kalshi-agent

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ufw caddy sqlite3 unattended-upgrades >/dev/null

say "Creating the '$APP_USER' user (the agent never runs as root)"
id $APP_USER >/dev/null 2>&1 || adduser --disabled-password --gecos "" $APP_USER
echo "$APP_USER ALL=(root) NOPASSWD: /bin/systemctl restart kalshi-collector kalshi-dashboard" > /etc/sudoers.d/kalshi
chmod 440 /etc/sudoers.d/kalshi

say "Firewall: allow only SSH and HTTPS"
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

say "GitHub read-only deploy key"
sudo -u $APP_USER mkdir -p $HOME_DIR/.ssh
if [ ! -f $HOME_DIR/.ssh/deploy_key ]; then
  sudo -u $APP_USER ssh-keygen -t ed25519 -N "" -C "kalshi-vps-deploy" -f $HOME_DIR/.ssh/deploy_key -q
fi
sudo -u $APP_USER tee $HOME_DIR/.ssh/config >/dev/null <<EOF
Host github.com
  IdentityFile ~/.ssh/deploy_key
  StrictHostKeyChecking accept-new
EOF
if [ ! -d $APP/.git ]; then
  echo
  echo "Add this PUBLIC key to GitHub (repo > Settings > Deploy keys > Add deploy key, leave 'write access' OFF):"
  echo
  cat $HOME_DIR/.ssh/deploy_key.pub
  echo
  read -rp "Press Enter after you've saved it on GitHub... "
  sudo -u $APP_USER git clone -q "$REPO" $APP
fi

say "Python environment (lives only on this server)"
sudo -u $APP_USER python3 -m venv $APP/.venv
sudo -u $APP_USER $APP/.venv/bin/pip install -q --upgrade pip
sudo -u $APP_USER $APP/.venv/bin/pip install -q -r $APP/requirements-dev.txt

say "Running the test suite (setup stops here if anything fails)"
sudo -u $APP_USER bash -c "cd $APP && .venv/bin/python -m pytest -q"

say "Secrets file"
if [ ! -f $APP/.env ]; then
  PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")
  sudo -u $APP_USER bash -c "umask 077; printf 'DASHBOARD_PASSWORD=%s\n' '$PW' > $APP/.env"
  NEW_PW=$PW
fi
sudo -u $APP_USER mkdir -p $APP/data $APP/logs $HOME_DIR/secrets
chmod 700 $HOME_DIR/secrets

say "Background services"
cp $APP/deploy/systemd/*.service $APP/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-collector kalshi-dashboard kalshi-update.timer kalshi-backup.timer

say "HTTPS dashboard"
IP=$(curl -4 -s https://api.ipify.org)
HOST="$(echo "$IP" | tr . -).sslip.io"
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
  encode gzip
  reverse_proxy 127.0.0.1:8080
}
EOF
systemctl reload caddy || systemctl restart caddy

sleep 5
say "Done"
echo "Dashboard:  https://$HOST"
echo "Username:   zeke (any name works)"
if [ -n "${NEW_PW:-}" ]; then
  echo "Password:   $NEW_PW"
  echo "            ^ save this in your password manager now. It is not shown again"
  echo "              (it's stored only in $APP/.env on this server)."
fi
echo
systemctl --no-pager --lines=0 status kalshi-collector kalshi-dashboard | grep -E "●|Active"
