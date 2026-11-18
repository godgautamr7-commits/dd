#!/usr/bin/env bash
# Node setup: bash install_node.sh <BOT_URL> <TOKEN>
#   BOT_URL = the bot's Cloudflare address, e.g. https://bot.yourdomain.com
#             (for a same-machine test you can also use localhost:8765 or <ip>:8765)
# The node connects OUT to the bot - no IP, no port, no firewall rule needed on this server.
set -e
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
PANEL="${1:?usage: bash install_node.sh <BOT_URL> <TOKEN>}"
TOKEN="${2:?usage: bash install_node.sh <BOT_URL> <TOKEN>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq python3-venv python3-pip iptables curl
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q aiohttp
cat > /etc/systemd/system/legacy-vps-node.service <<UNIT
[Unit]
Description=Legacy VPS node agent
After=network-online.target snap.lxd.daemon.service
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
Environment=PYTHONUNBUFFERED=1
Environment=TOKEN=$TOKEN
Environment=PANEL_URL=$PANEL
ExecStart=$DIR/venv/bin/python $DIR/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now legacy-vps-node
echo "Node agent started - it connects to $PANEL by itself. Logs: journalctl -u legacy-vps-node -f"
echo "In Discord the node shows 🟢 online within a few seconds (check /node-list)."
