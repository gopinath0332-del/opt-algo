"""Notification manager for Options trading."""

from typing import Optional

from core.config import Config
from core.logger import get_logger
from .discord import DiscordNotifier

logger = get_logger(__name__)


class NotificationManager:
    """Manages all notification channels for options trading."""

    def __init__(self, config: Config):
        """Initialize Notification Manager.

        Args:
            config: Application configuration
        """
        self.config = config

        # Initialize Discord
        self.discord: Optional[DiscordNotifier] = None
        if config.discord_enabled and config.discord_webhook_url:
            self.discord = DiscordNotifier(config.discord_webhook_url)
            logger.info("Discord notifications enabled")

        # Separate error webhook
        self.discord_error: Optional[DiscordNotifier] = None
        if config.discord_enabled and config.discord_error_webhook_url:
            self.discord_error = DiscordNotifier(config.discord_error_webhook_url)
            logger.info("Discord error notifications enabled (separate webhook)")
        elif config.discord_enabled and config.discord_webhook_url:
            self.discord_error = self.discord
            logger.info("Discord error notifications will use main webhook")

    def send_entry_alert(self, **kwargs) -> None:
        """Send straddle entry alert to all enabled channels."""
        if self.discord and self.config.notifications.alert_on_entry:
            self.discord.send_entry_alert(**kwargs)
            logger.info("Entry alert sent")

    def send_exit_alert(self, **kwargs) -> None:
        """Send straddle exit alert to all enabled channels."""
        if self.discord and self.config.notifications.alert_on_exit:
            self.discord.send_exit_alert(**kwargs)
            logger.info("Exit alert sent")

    def send_sl_alert(self, **kwargs) -> None:
        """Send stop-loss hit alert to all enabled channels."""
        if self.discord and self.config.notifications.alert_on_sl_hit:
            self.discord.send_sl_alert(**kwargs)
            logger.info("Stop-loss alert sent")

    def send_error(self, title: str, error: str) -> None:
        """Send error alert to error webhook."""
        if self.discord_error and self.config.notifications.alert_on_error:
            self.discord_error.send_error(title, error)

    def send_status_message(self, title: str, message: str, color: int = 3447003) -> None:
        """Send a status message."""
        if self.discord:
            self.discord.send_status_message(title, message, color)
            logger.info(f"Status message sent: {title}")
