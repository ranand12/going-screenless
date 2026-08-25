#!/usr/bin/env bash
set -euo pipefail

# Sample: Deployment script for a Pi-based voice agent.
# Shows the general structure — adapt paths and services to your setup.
#
# Usage:
#   ./deploy_sample.sh [user@pi-host]

PI="${1:-user@your-pi-hostname}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying Voice Agent to ${PI} ==="

# 1. Copy the voice agent script
echo "[1/5] Copying voice agent script..."
scp "${SCRIPT_DIR}/voice_agent.py" "${PI}:~/voice_agent.py"

# 2. Install Python dependencies in agent's virtualenv
echo "[2/5] Installing Python dependencies..."
ssh "${PI}" 'VIRTUAL_ENV=~/.agent/venv PATH="$VIRTUAL_ENV/bin:$PATH" \
  pip install --quiet websockets sounddevice numpy openwakeword'

# 3. Install system packages for media playback
echo "[3/5] Installing system packages..."
ssh "${PI}" 'sudo apt-get install -y -qq mpv 2>/dev/null; \
  pip install --break-system-packages -q -U yt-dlp 2>/dev/null'

# 4. Deploy systemd user service
echo "[4/5] Setting up systemd service..."
ssh "${PI}" 'mkdir -p ~/.config/systemd/user'
cat <<'SERVICE_EOF' | ssh "${PI}" 'cat > ~/.config/systemd/user/voice-agent.service'
[Unit]
Description=Voice Agent (Wake Word + Gemini Live)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.agent/venv/bin/python3 %h/voice_agent.py
Restart=always
RestartSec=5
# Load API keys from environment file
EnvironmentFile=%h/.agent/.env

[Install]
WantedBy=default.target
SERVICE_EOF

ssh "${PI}" 'systemctl --user daemon-reload && \
  systemctl --user enable voice-agent.service && \
  systemctl --user restart voice-agent.service'

# 5. Enable linger so services run without active login
echo "[5/5] Enabling linger..."
ssh "${PI}" 'sudo loginctl enable-linger $(whoami)'

echo "=== Deployment complete ==="
echo "Check status: ssh ${PI} 'systemctl --user status voice-agent'"
echo "View logs:    ssh ${PI} 'journalctl --user -u voice-agent -f'"
