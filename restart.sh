#!/bin/bash

# Restart script for Delta Exchange Bots (Options Bot & Gold ORB) on Raspberry Pi / Linux

OPTIONS_SERVICE="options-bot"
GOLD_SERVICE="gold_orb"

echo "=========================================================="
echo "Restarting Delta Exchange Trading Bot Services..."
echo "=========================================================="

# 1. Restart options-bot systemd service
if systemctl list-unit-files | grep -q "^$OPTIONS_SERVICE.service"; then
    echo "Restarting $OPTIONS_SERVICE..."
    sudo systemctl restart $OPTIONS_SERVICE
fi

# 2. Restart gold_orb systemd service
if systemctl list-unit-files | grep -q "^$GOLD_SERVICE.service"; then
    echo "Restarting $GOLD_SERVICE..."
    sudo systemctl restart $GOLD_SERVICE
fi

# 3. Wait a moment for startup
sleep 2

# 4. Check services status
echo ""
echo "Service Statuses:"
sudo systemctl status $OPTIONS_SERVICE $GOLD_SERVICE --no-pager || true

# 5. Show last 15 lines of combined logs
echo ""
echo "Recent Logs:"
sudo journalctl -u $OPTIONS_SERVICE -u $GOLD_SERVICE -n 15 --no-pager

echo "=========================================================="

