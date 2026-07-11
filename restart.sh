#!/bin/bash

# Restart script for Delta Exchange Options Bot on Raspberry Pi / Linux

SERVICE_NAME="options-bot"

echo "=========================================================="
echo "Restarting Options Bot Service..."
echo "=========================================================="

# 1. Restart systemd service
sudo systemctl restart $SERVICE_NAME

# 2. Wait a moment for startup
sleep 2

# 3. Check service status
echo ""
echo "Service Status:"
sudo systemctl status $SERVICE_NAME --no-pager

# 4. Show last 15 lines of logs
echo ""
echo "Recent Logs:"
sudo journalctl -u $SERVICE_NAME -n 15 --no-pager

echo "=========================================================="
