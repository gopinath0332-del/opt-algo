#!/bin/bash

# Deployment script for Delta Exchange Options Bot on Raspberry Pi / Linux

set -e

PROJECT_DIR="/home/pi/opt-algo"
SERVICE_NAME="options-bot"

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

# 4. Copy project files (assuming run from the cloned repository directory)
echo "Step 4: Copying application files..."
cp -R api core strategy notifications config main.py requirements.txt "$PROJECT_DIR/"

# Setup default configuration files if they don't exist
if [ ! -f "$PROJECT_DIR/config/.env" ]; then
    echo "Creating empty .env configuration file..."
    cp "$PROJECT_DIR/config/.env.example" "$PROJECT_DIR/config/.env"
    echo "⚠️  Please edit $PROJECT_DIR/config/.env with your production credentials."
fi

# 5. Install Python dependencies
echo "Step 5: Installing Python requirements..."
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# 6. Setup Systemd service
echo "Step 6: Configuring systemd service..."
sudo cp "$PROJECT_DIR/config/options-bot.service" "/etc/systemd/system/$SERVICE_NAME.service"
sudo systemctl daemon-reload

echo "=========================================================="
echo "Deployment structure complete!"
echo "=========================================================="
echo "Next steps:"
echo "  1. Edit your production configuration: nano $PROJECT_DIR/config/.env"
echo "  2. Test the bot once in paper/dry-run mode:"
echo "     $PROJECT_DIR/venv/bin/python $PROJECT_DIR/main.py --once --paper"
echo "  3. Start the background service:"
echo "     sudo systemctl enable $SERVICE_NAME"
echo "     sudo systemctl start $SERVICE_NAME"
echo "  4. Check the service status and logs:"
echo "     sudo systemctl status $SERVICE_NAME"
echo "     journalctl -u $SERVICE_NAME -f"
echo "=========================================================="
