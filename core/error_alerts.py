"""Error alert handler — sends ERROR/CRITICAL log messages to Discord."""

import logging
import re
import time
from datetime import datetime
from typing import Dict, Optional

import requests


class ErrorAlertHandler(logging.Handler):
    """Custom logging handler that sends alerts for ERROR and CRITICAL messages.

    Supports Discord webhooks with throttling to prevent spam.
    """

    def __init__(
        self,
        discord_webhook_url: Optional[str] = None,
        alert_throttle_seconds: int = 300,
        min_level: int = logging.ERROR,
    ):
        """Initialize the error alert handler.

        Args:
            discord_webhook_url: Discord webhook URL for alerts
            alert_throttle_seconds: Minimum seconds between alerts for same error
            min_level: Minimum log level to trigger alerts (default: ERROR)
        """
        super().__init__()
        self.discord_webhook_url = discord_webhook_url
        self.alert_throttle_seconds = alert_throttle_seconds
        self.min_level = min_level

        # Track last alert time for each error type to prevent spam
        self._last_alert_times: Dict[str, datetime] = {}

        # Set handler level
        self.setLevel(min_level)

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record by sending alerts.

        Args:
            record: Log record to process
        """
        try:
            if record.levelno < self.min_level:
                return

            # Create a throttle key from the error message
            message = record.getMessage()
            # Strip ANSI codes and dynamic values for consistent throttling
            clean_msg = re.sub(r'\x1B\[[0-9;]*m', '', message)
            throttle_key = f"{record.name}:{clean_msg[:100]}"

            # Check throttle
            now = datetime.utcnow()
            last_alert = self._last_alert_times.get(throttle_key)
            if last_alert:
                elapsed = (now - last_alert).total_seconds()
                if elapsed < self.alert_throttle_seconds:
                    return

            # Update throttle timestamp
            self._last_alert_times[throttle_key] = now

            # Send to Discord
            if self.discord_webhook_url:
                self._send_discord_alert(record, message)

        except Exception:
            # Never let alert sending break the application
            pass

    def _send_discord_alert(self, record: logging.LogRecord, message: str) -> None:
        """Send an error alert to Discord.

        Args:
            record: Log record
            message: Formatted message
        """
        level_emoji = "🔴" if record.levelno >= logging.CRITICAL else "⚠️"
        level_name = record.levelname

        # Build Discord embed
        embed = {
            "title": f"{level_emoji} [{level_name}] Options Bot Alert",
            "description": f"```\n{message[:2000]}\n```",
            "color": 15158332 if record.levelno >= logging.CRITICAL else 15105570,
            "fields": [
                {"name": "Module", "value": record.name, "inline": True},
                {"name": "Time (UTC)", "value": time.strftime("%H:%M:%S", time.gmtime()), "inline": True},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        payload = {"embeds": [embed]}

        try:
            response = requests.post(self.discord_webhook_url, json=payload, timeout=5)
            response.raise_for_status()
        except Exception:
            pass  # Silently ignore alert delivery failures


def create_error_alert_handler(
    discord_webhook_url: Optional[str] = None,
    alert_throttle_seconds: int = 300,
) -> Optional[ErrorAlertHandler]:
    """Factory function to create an error alert handler.

    Args:
        discord_webhook_url: Discord webhook URL
        alert_throttle_seconds: Throttle interval

    Returns:
        ErrorAlertHandler instance or None if no webhook configured
    """
    if not discord_webhook_url:
        return None

    return ErrorAlertHandler(
        discord_webhook_url=discord_webhook_url,
        alert_throttle_seconds=alert_throttle_seconds,
    )
