#!/bin/bash
# Know Your City VM startup script (runs as root on every boot)
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq git python3 python3-venv >/dev/null 2>&1
mkdir -p /opt/kyc/data
if [ -d /opt/kyc/src/.git ]; then git -C /opt/kyc/src pull -q; else git clone -q https://github.com/dkadam959-lgtm/knowyourcity-bot.git /opt/kyc/src; fi
cp /opt/kyc/src/bot.py /opt/kyc/bot.py
[ -f /opt/kyc/data/questions.csv ] || cp /opt/kyc/src/questions.csv /opt/kyc/data/questions.csv
[ -d /opt/kyc/venv ] || python3 -m venv /opt/kyc/venv
/opt/kyc/venv/bin/pip install -q -r /opt/kyc/src/requirements.txt
cat > /opt/kyc/setenv.sh <<'EOF'
#!/bin/bash
read -rp "BOT_TOKEN: " T; read -rp "CHANNEL_ID (@KnowYourCitypune): " C; read -rp "ADMIN_IDS: " A; read -rp "START_DATE (2026-10-27): " S; read -rp "SHEET_URL (Google Sheet link, Enter for default): " G
printf "BOT_TOKEN=%s\nCHANNEL_ID=%s\nADMIN_IDS=%s\nSTART_DATE=%s\nSHEET_URL=%s\nSPONSOR_TEXT=\nDATA_DIR=/opt/kyc/data\nTZ_NAME=Asia/Kolkata\nQUIZ_HOUR=8\nREMIND_HOUR=15\nRESULT_HOUR=21\nPOLL_SECONDS=30\n" "$T" "${C:-@KnowYourCitypune}" "$A" "${S:-2026-10-27}" "${G:-https://docs.google.com/spreadsheets/d/1JbJia_6bIlj73xjdgY3JiaCqAbYGQAGHgD297CJguf0/edit}" > /opt/kyc/.env
chmod 600 /opt/kyc/.env; systemctl restart kyc-bot; sleep 3; systemctl --no-pager status kyc-bot | head -n 6
EOF
chmod +x /opt/kyc/setenv.sh
cat > /etc/systemd/system/kyc-bot.service <<'EOF'
[Unit]
Description=Know Your City Telegram bot
After=network-online.target
Wants=network-online.target
ConditionPathExists=/opt/kyc/.env
[Service]
WorkingDirectory=/opt/kyc
EnvironmentFile=/opt/kyc/.env
ExecStart=/opt/kyc/venv/bin/python /opt/kyc/bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable kyc-bot >/dev/null 2>&1 && systemctl restart kyc-bot
echo "KYC startup done $(date)" >> /var/log/kyc-startup.log
