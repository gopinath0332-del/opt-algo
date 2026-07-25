#!/bin/bash

# Restart script for Delta Exchange Bots (Options Bot & Gold ORB) on Raspberry Pi / Linux
# NOTE: Gold ORB is currently DISABLED. Uncomment the gold_orb block below to re-enable.

OPTIONS_SERVICE="options-bot"
GOLD_SERVICE="gold_orb"
GOLD_ORB_ENABLED=false   # Set to true to re-enable Gold ORB

echo "=========================================================="
echo "Restarting Delta Exchange Trading Bot Services..."
echo "=========================================================="

# 1. Restart options-bot systemd service
if systemctl list-unit-files | grep -q "^$OPTIONS_SERVICE.service"; then
    echo "Restarting $OPTIONS_SERVICE..."
    sudo systemctl restart $OPTIONS_SERVICE
else
    echo "WARNING: $OPTIONS_SERVICE service not found, skipping."
fi

# 2. Gold ORB service (currently disabled)
if [ "$GOLD_ORB_ENABLED" = true ]; then
    if systemctl list-unit-files | grep -q "^$GOLD_SERVICE.service"; then
        echo "Restarting $GOLD_SERVICE..."
        sudo systemctl restart $GOLD_SERVICE
    else
        echo "WARNING: $GOLD_SERVICE service not found, skipping."
    fi
else
    echo "Skipping $GOLD_SERVICE (GOLD_ORB_ENABLED=false)."
fi

# 3. Wait a moment for startup
sleep 2

# 4. Check services status
echo ""
echo "Service Statuses:"
if [ "$GOLD_ORB_ENABLED" = true ]; then
    sudo systemctl status $OPTIONS_SERVICE $GOLD_SERVICE --no-pager || true
else
    sudo systemctl status $OPTIONS_SERVICE --no-pager || true
fi

# 5. Show recent logs
echo ""
echo "Recent Logs:"
if [ "$GOLD_ORB_ENABLED" = true ]; then
    sudo journalctl -u $OPTIONS_SERVICE -u $GOLD_SERVICE -n 15 --no-pager
else
    sudo journalctl -u $OPTIONS_SERVICE -n 15 --no-pager
fi

echo "=========================================================="

