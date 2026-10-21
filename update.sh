#!/usr/bin/env bash
# Pull latest code from GitHub and restart the bot. Run ON THE VM: bash /opt/kyc/update.sh
set -e
cd /opt/kyc/src && git pull -q
cp /opt/kyc/src/bot.py /opt/kyc/bot.py
/opt/kyc/venv/bin/pip install -q -r /opt/kyc/src/requirements.txt
sudo systemctl restart kyc-bot
sleep 2
sudo systemctl --no-pager status kyc-bot | head -n 5
