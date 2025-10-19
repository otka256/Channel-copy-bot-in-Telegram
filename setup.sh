#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "⚡ Transfer Bot setup starting in: $ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
	echo "📦 Installing python3 and system packages (requires sudo)..."
	sudo apt update
	sudo apt install -y python3 python3-venv python3-pip ffmpeg
else
	echo "python3 found"
fi

python3 -m venv "$ROOT_DIR/venv"
echo "Virtualenv created at $ROOT_DIR/venv"

echo "Activating virtualenv and installing Python packages..."
source "$ROOT_DIR/venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$ROOT_DIR/requirements.txt"

read -p "Do you want to create a .env now? [y/N]: " createenv
if [[ "$createenv" =~ ^[Yy]$ ]]; then
	read -p "API_ID (numeric): " API_ID
	read -p "API_HASH: " API_HASH
	read -p "BOT_TOKEN: " BOT_TOKEN
	read -p "ADMIN_ID (numeric): " ADMIN_ID
	cat > "$ROOT_DIR/.env" <<EOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
ADMIN_ID=$ADMIN_ID
EOF
	chmod 600 "$ROOT_DIR/.env" || true
	echo ".env created"
else
	echo "Skipping .env creation. Make sure a .env file exists before starting the service."
fi

echo "Next step: run the bot once to complete user login (if required)."
read -p "Run bot now in foreground to allow session generation? [y/N]: " runnow
if [[ "$runnow" =~ ^[Yy]$ ]]; then
	python "$ROOT_DIR/bot.py"
	echo "If the bot required interactive login, it should have completed (Ctrl+C to stop)."
fi

SERVICE_FILE="/etc/systemd/system/transferbot.service"
echo "Creating systemd service at $SERVICE_FILE"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Transfer Bot Service
After=network.target

[Service]
User=$USER
WorkingDirectory=$ROOT_DIR
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=$ROOT_DIR/.env
ExecStart=$ROOT_DIR/venv/bin/python $ROOT_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable transferbot
sudo systemctl restart transferbot
echo "Service started. Check logs with: sudo journalctl -u transferbot -f"

echo "Setup finished."
