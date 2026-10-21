#!/usr/bin/env bash
# Know Your City bot. One-shot setup on a fresh Google Cloud VM (Debian/Ubuntu).
# Run ON THE VM:  bash deploy_gcp.sh
# It installs Python, copies the bot to /opt/kyc, asks for settings, writes /opt/kyc/.env
# and installs a systemd service that restarts the bot on crash and on reboot.
set -euo pipefail

APP_DIR=/opt/kyc
DATA_DIR=/opt/kyc/data
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/5 packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

echo "== 2/5 copy code to $APP_DIR"
sudo mkdir -p "$APP_DIR" "$DATA_DIR"
sudo cp "$SRC_DIR"/bot.py "$SRC_DIR"/requirements.txt "$APP_DIR"/
[ -f "$SRC_DIR/questions.csv" ] && sudo cp "$SRC_DIR/questions.csv" "$APP_DIR"/
sudo chown -R "$USER":"$USER" "$APP_DIR"

echo "== 3/5 python environment"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== 4/5 settings"
if [ ! -f "$APP_DIR/.env" ]; then
  read -rp "BOT_TOKEN (BotFather): " BOT_TOKEN
  read -rp "CHANNEL_ID (e.g. @KnowYourCity): " CHANNEL_ID
  read -rp "ADMIN_IDS (your Telegram user id, comma separated): " ADMIN_IDS
  read -rp "START_DATE (YYYY-MM-DD launch date): " START_DATE
  read -rp "SPONSOR_TEXT (leave empty if none): " SPONSOR_TEXT
  cat > "$APP_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
CHANNEL_ID=$CHANNEL_ID
ADMIN_IDS=$ADMIN_IDS
START_DATE=$START_DATE
SPONSOR_TEXT=$SPONSOR_TEXT
DATA_DIR=$DATA_DIR
TZ_NAME=Asia/Kolkata
QUIZ_HOUR=8
REMIND_HOUR=15
RESULT_HOUR=21
POLL_SECONDS=30
EOF
  chmod 600 "$APP_DIR/.env"
  echo "saved $APP_DIR/.env"
else
  echo "using existing $APP_DIR/.env"
fi

echo "== 5/5 systemd service"
sudo tee /etc/systemd/system/kyc-bot.service >/dev/null <<EOF
[Unit]
Description=Know Your City Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable kyc-bot >/dev/null
sudo systemctl restart kyc-bot
sleep 3
sudo systemctl --no-pager status kyc-bot | head -n 12
echo
echo "DONE. Logs:  sudo journalctl -u kyc-bot -f"
echo "Restart:    sudo systemctl restart kyc-bot"
echo "Update code: copy new bot.py to $APP_DIR then restart."
