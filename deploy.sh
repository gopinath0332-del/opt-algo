#!/bin/bash

# Deployment script for Delta Exchange Options Bot on Raspberry Pi / Linux
# NOTE: Gold ORB is currently DISABLED. Set GOLD_ORB_ENABLED=true below to re-enable.

set -e

PROJECT_DIR="/home/pi/opt-algo"
SERVICE_NAME="options-bot"
GOLD_ORB_ENABLED=false   # Set to true to enable Gold ORB service on deploy

echo "=========================================================="
echo "Starting Delta Exchange Options Bot Deployment"
echo "=========================================================="

# 1. Update and install base packages
echo "Step 1: Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv git

# 2. Setup project directory
if [ ! -d "$PROJECT_DIR" ]; then
    echo "Step 2: Creating directory $PROJECT_DIR..."
    sudo mkdir -p "$PROJECT_DIR"
    sudo chown pi:pi "$PROJECT_DIR"
fi

# 3. Setup virtual environment
echo "Step 3: Setting up python virtual environment..."
if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi

# Activate venv and upgrade pip
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip

# 4. Copy project files if not already in target directory
if [ "$(pwd)" != "$PROJECT_DIR" ]; then
    echo "Step 4: Copying application files..."
    cp -R api core strategy notifications config main.py run_gold_orb.py requirements.txt "$PROJECT_DIR/"
else
    echo "Step 4: Already in target directory, skipping copy."
fi

# Setup default configuration files if they don't exist
if [ ! -f "$PROJECT_DIR/config/.env" ]; then
    echo "Creating empty .env configuration file..."
    cp "$PROJECT_DIR/config/.env.example" "$PROJECT_DIR/config/.env"
    echo "⚠️  Please edit $PROJECT_DIR/config/.env with your production credentials."
fi

# 5. Install Python dependencies
echo "Step 5: Installing Python requirements..."
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# 6. Setup Systemd services
echo "Step 6: Configuring systemd services..."
sudo cp "$PROJECT_DIR/config/options-bot.service" "/etc/systemd/system/options-bot.service"

if [ -f "$PROJECT_DIR/config/gold_orb.service" ]; then
    sudo cp "$PROJECT_DIR/config/gold_orb.service" "/etc/systemd/system/gold_orb.service"
    echo "Gold ORB service file installed (not enabled — GOLD_ORB_ENABLED=$GOLD_ORB_ENABLED)."
fi

sudo systemctl daemon-reload

# 7. Enable & start services
echo "Step 7: Enabling and starting services..."
sudo systemctl enable --now options-bot
echo "✅ options-bot enabled and started."

if [ "$GOLD_ORB_ENABLED" = true ]; then
    sudo systemctl enable --now gold_orb
    echo "✅ gold_orb enabled and started."
else
    # Make sure gold_orb is stopped and disabled
    sudo systemctl stop gold_orb 2>/dev/null || true
    sudo systemctl disable gold_orb 2>/dev/null || true
    echo "⏸️  gold_orb is disabled (GOLD_ORB_ENABLED=false). Skipped."
fi

echo "=========================================================="
echo "Deployment complete!"
echo "=========================================================="
echo "Next steps:"
echo "  1. Verify config:  nano $PROJECT_DIR/config/.env"
echo "  2. Test options bot (dry-run):"
echo "     $PROJECT_DIR/venv/bin/python $PROJECT_DIR/main.py --once --paper"
echo "  3. Check service status and logs:"
echo "     sudo systemctl status options-bot"
echo "     journalctl -u options-bot -f"
if [ "$GOLD_ORB_ENABLED" = false ]; then
    echo ""
    echo "  To re-enable Gold ORB later:"
    echo "     Set GOLD_ORB_ENABLED=true in deploy.sh and re-run, OR manually:"
    echo "     sudo systemctl enable --now gold_orb"
fi
echo "=========================================================="

