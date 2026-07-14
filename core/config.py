"""Configuration management for the Delta Exchange Options trading platform."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .exceptions import ValidationError
from .logger import get_logger

logger = get_logger(__name__)


class StopLossConfig(BaseModel):
    """Stop-loss configuration."""

    type: str = Field(default="premium_pct", description="SL type: premium_pct or max_loss_usd")
    value: float = Field(default=50.0, gt=0, description="SL value (50 = 50% of premium)")


class StrategyConfig(BaseModel):
    """Strategy configuration."""

    name: str = Field(default="short_straddle")
    underlying: str = Field(default="BTC")
    entry_time: str = Field(default="17:00")
    exit_time: str = Field(default="17:25")
    timezone: str = Field(default="Asia/Kolkata")
    lot_size: Optional[int] = Field(default=None, gt=0)           # Static lot size (overrides dynamic sizing if set)
    capital_allocation_pct: float = Field(default=60.0, gt=0, le=100)  # % of balance to deploy per trade
    leverage: int = Field(default=200, gt=0)
    order_type: str = Field(default="market_order")
    stop_loss: Optional[StopLossConfig] = Field(default=None)
    monitor_interval_sec: int = Field(default=5, gt=0)


class NotificationsConfig(BaseModel):
    """Notifications configuration."""

    discord_enabled: bool = True
    alert_on_entry: bool = True
    alert_on_exit: bool = True
    alert_on_sl_hit: bool = True
    alert_on_error: bool = True


class Config:
    """Main configuration class for the options trading platform."""

    def __init__(self, env_file: Optional[str] = None, settings_file: Optional[str] = None):
        """Initialize configuration.

        Args:
            env_file: Path to .env file (default: config/.env)
            settings_file: Path to settings.yaml file (default: config/settings.yaml)
        """
        self.project_root = Path(__file__).parent.parent

        # Load environment variables
        if env_file is None:
            env_file = str(self.project_root / "config" / ".env")

        if Path(env_file).exists():
            load_dotenv(env_file)
            logger.info("Loaded environment variables", file=str(env_file))
        else:
            logger.warning("Environment file not found", file=str(env_file))

        # Load settings from YAML
        if settings_file is None:
            settings_file = str(self.project_root / "config" / "settings.yaml")

        self.settings = self._load_settings(Path(settings_file))

        # Initialize configuration sections
        self._init_api_config()
        self._init_notification_config()
        self._init_logging_config()
        self._init_strategy_config()
        self._init_firestore_config()

        logger.info("Configuration initialized successfully")

    def _load_settings(self, settings_file: Path) -> Dict[str, Any]:
        """Load settings from YAML file."""
        if not Path(settings_file).exists():
            logger.warning("Settings file not found, using defaults", file=str(settings_file))
            return {}

        try:
            with open(settings_file, "r") as f:
                settings = yaml.safe_load(f)
                logger.info("Loaded settings from YAML", file=str(settings_file))
                return settings or {}
        except Exception as e:
            logger.error("Failed to load settings file", file=str(settings_file), error=str(e))
            raise ValidationError(f"Failed to load settings: {e}")

    def _init_api_config(self):
        """Initialize API configuration."""
        self.api_key = os.getenv("DELTA_API_KEY", "")
        self.api_secret = os.getenv("DELTA_API_SECRET", "")
        self.environment = os.getenv("DELTA_ENVIRONMENT", "testnet")
        self.base_url = os.getenv("DELTA_BASE_URL", "https://cdn-ind.testnet.deltaex.org")

        if not self.api_key or not self.api_secret:
            logger.warning("API credentials not set in environment variables")

    def _init_notification_config(self):
        """Initialize notification configuration."""
        self.discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.discord_error_webhook_url = os.getenv("DISCORD_ERROR_WEBHOOK_URL", "")
        self.discord_enabled = os.getenv("DISCORD_ENABLED", "true").lower() == "true"

        # Load notification settings from YAML
        notifications_settings = self.settings.get("notifications", {})
        self.notifications = NotificationsConfig(**notifications_settings)

    def _init_logging_config(self):
        """Initialize logging configuration."""
        default_log_level = "DEBUG" if self.environment == "testnet" else "INFO"
        self.log_level = os.getenv("LOG_LEVEL", default_log_level)
        self.log_file = os.getenv("LOG_FILE", "logs/options.log")
        self.log_max_bytes = int(os.getenv("LOG_MAX_BYTES", "524288000"))  # 500MB
        self.log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))
        self.enable_error_alerts = os.getenv("ENABLE_ERROR_ALERTS", "true").lower() == "true"
        self.alert_throttle_seconds = int(os.getenv("ALERT_THROTTLE_SECONDS", "300"))

    def _init_strategy_config(self):
        """Initialize strategy configuration."""
        strategy_settings = self.settings.get("strategy", {})

        # Handle nested stop_loss config
        if "stop_loss" in strategy_settings and isinstance(strategy_settings["stop_loss"], dict):
            strategy_settings["stop_loss"] = StopLossConfig(**strategy_settings["stop_loss"])

        self.strategy = StrategyConfig(**strategy_settings)

        # Order placement kill-switch
        self.enable_order_placement = os.getenv("ENABLE_ORDER_PLACEMENT", "false").lower() == "true"

    def _init_firestore_config(self):
        """Initialize Firestore configuration for trade journaling."""
        from core.firestore_client import initialize_firestore

        firestore_settings = self.settings.get("firestore", {})

        self.firestore_enabled = firestore_settings.get("enabled", True)
        self.firestore_service_account_path = firestore_settings.get(
            "service_account_path",
            "config/firestore-service-account.json"
        )
        self.firestore_collection_name = firestore_settings.get("collection_name", "options")

        if self.firestore_enabled:
            # Convert relative path to absolute
            if not os.path.isabs(self.firestore_service_account_path):
                self.firestore_service_account_path = os.path.join(
                    self.project_root,
                    self.firestore_service_account_path
                )

            success = initialize_firestore(
                service_account_path=self.firestore_service_account_path,
                collection_name=self.firestore_collection_name,
                enabled=self.firestore_enabled
            )

            if success:
                logger.info("Firestore trade journaling initialized",
                           collection=self.firestore_collection_name)
            else:
                logger.warning("Firestore trade journaling disabled due to initialization failure")
        else:
            logger.info("Firestore trade journaling is disabled in configuration")

    def get_mode(self) -> str:
        """Get the trading mode based on environment.

        Returns:
            'live' for production, 'paper' for testnet
        """
        if self.environment.lower() == "production":
            return "live"
        return "paper"

    def is_testnet(self) -> bool:
        """Check if running in testnet mode."""
        return self.environment.lower() == "testnet"

    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.environment.lower() == "production"

    def validate(self) -> bool:
        """Validate configuration.

        Returns:
            True if configuration is valid

        Raises:
            ValidationError: If configuration is invalid
        """
        errors = []

        if not self.api_key:
            errors.append("DELTA_API_KEY is not set")
        if not self.api_secret:
            errors.append("DELTA_API_SECRET is not set")

        if self.discord_enabled and not self.discord_webhook_url:
            errors.append("Discord is enabled but DISCORD_WEBHOOK_URL is not set")

        if errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            logger.error("Configuration validation failed", errors=errors)
            raise ValidationError(error_msg)

        logger.info("Configuration validation successful")
        return True

    def __repr__(self) -> str:
        """Return string representation of configuration."""
        return (
            f"Config(environment={self.environment}, "
            f"base_url={self.base_url}, "
            f"strategy={self.strategy.name}, "
            f"underlying={self.strategy.underlying})"
        )


# Global configuration instance
_config: Optional[Config] = None


def get_config(env_file: Optional[str] = None, settings_file: Optional[str] = None) -> Config:
    """Get or create global configuration instance.

    Args:
        env_file: Path to .env file
        settings_file: Path to settings.yaml file

    Returns:
        Configuration instance
    """
    global _config
    if _config is None:
        _config = Config(env_file=env_file, settings_file=settings_file)
    return _config
