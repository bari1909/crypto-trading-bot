#!/usr/bin/env bash
# Copy the bot to the server and (re)start it. Run from the project root on your PC:
#   SERVER=1.2.3.4 bash deploy/deploy.sh
# Uploads code + .env (mode 600), then `docker compose up -d --build` as user "trader".
set -euo pipefail

SERVER="${SERVER:?set SERVER=<ip>}"
KEY="${KEY:-$HOME/.ssh/scalper_server}"
TARGET="trader@$SERVER"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$TARGET")

[ -f .env ] || { echo "Run from the project root (no .env here)"; exit 1; }

echo "== uploading code"
tar czf - --exclude=./.env --exclude=./data --exclude=./_old_v1 --exclude='*/__pycache__' \
    --exclude=./.pytest_cache --exclude=./.git . \
  | "${SSH[@]}" 'mkdir -p ~/bot ~/bot/data && tar xzf - -C ~/bot'

echo "== uploading .env (private)"
"${SSH[@]}" 'umask 077 && cat > ~/bot/.env' < .env

echo "== building and starting"
"${SSH[@]}" 'cd ~/bot && docker compose up -d --build --remove-orphans && docker compose ps --format "{{.Service}}: {{.Status}}"'

echo
echo "Deployed. Dashboards (private, through an SSH tunnel):"
echo "  ssh -i $KEY -N -L 8501:localhost:8501 -L 8502:localhost:8502 $TARGET"
echo "  then open http://localhost:8501 (spot) and http://localhost:8502 (futures)"
