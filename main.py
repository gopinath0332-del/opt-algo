"""Main entry point for the Delta Exchange Options trading platform.

Runs the Short Straddle strategy on a daily schedule at 17:00 IST.

Usage:
    # Run once immediately (for testing):
    python main.py --once

    # Run once without placing orders (signal mode):
    python main.py --once --paper

    # Run on daily schedule:
    python main.py

    # Run on daily schedule without orders:
    python main.py --paper
"""

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.config import get_config
from core.exceptions import DeltaExchangeError
from core.logger import get_logger, setup_logging

IST = timezone(timedelta(hours=5, minutes=30))


def setup_environment(paper_mode: bool = False):
    """Set up logging and configuration.

    Args:
        paper_mode: If True, force order placement to disabled

    Returns:
        Tuple of (config, logger)
    """
    config = get_config()

    # Setup logging with error alerting
    setup_logging(
        log_level=config.log_level,
        log_file=config.log_file,
        log_max_bytes=config.log_max_bytes,
        log_backup_count=config.log_backup_count,
        discord_error_webhook_url=config.discord_error_webhook_url if config.discord_enabled else None,
        alert_throttle_seconds=config.alert_throttle_seconds,
        enable_error_alerts=config.enable_error_alerts,
    )

    logger = get_logger(__name__)

    # Override order placement if paper mode
    if paper_mode:
        config.enable_order_placement = False
        logger.info("Paper mode enabled — orders will NOT be placed")

    logger.info(
        "Delta Exchange Options Trading Platform",
        version="1.0.0",
        environment=config.environment,
        mode=config.get_mode(),
        underlying=config.strategy.underlying,
        order_placement=config.enable_order_placement,
    )

    return config, logger


def run_strategy(config, logger):
    """Execute the strategy once.

    Args:
        config: Configuration instance
        logger: Logger instance
    """
    from api.rest_client import DeltaRestClient
    from notifications.manager import NotificationManager
    from strategy.short_straddle import ShortStraddleStrategy

    # Initialize components
    client = DeltaRestClient(config)
    notifier = NotificationManager(config)

    # Send startup notification
    mode = config.get_mode()
    now = datetime.now(IST)

    startup_msg = (
        f"Strategy: \u001b[1;37mShort Straddle\u001b[0m\n"
        f"Underlying: \u001b[1;37m{config.strategy.underlying}\u001b[0m\n"
        f"Environment: \u001b[1;37m{config.environment}\u001b[0m\n"
        f"Mode: \u001b[1;37m{mode}\u001b[0m\n"
        f"Order Placement: \u001b[{'1;32' if config.enable_order_placement else '0;31'}m"
        f"{'ENABLED' if config.enable_order_placement else 'DISABLED'}\u001b[0m\n"
        f"Lot Size: \u001b[0;36m{config.strategy.lot_size}\u001b[0m per leg\n"
        f"Leverage: \u001b[0;35m{config.strategy.leverage}x\u001b[0m\n"
        f"SL: \u001b[0;33m{config.strategy.stop_loss.value}%\u001b[0m of premium\n"
        f"Entry: \u001b[0;36m{config.strategy.entry_time} IST\u001b[0m\n"
        f"Exit: \u001b[0;36m{config.strategy.exit_time} IST\u001b[0m\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S IST')}"
    )

    notifier.send_status_message(
        f"🚀 Options Bot Starting — {config.strategy.underlying}",
        startup_msg,
        color=5763719 if config.enable_order_placement else 15548997,
    )

    # Create and run strategy
    strategy = ShortStraddleStrategy(config, client, notifier)
    strategy.run()

    logger.info("Strategy execution complete")


def main():
    """Run the main application."""
    parser = argparse.ArgumentParser(
        description="Delta Exchange Options Trading — Short Straddle Strategy"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the strategy once immediately (don't schedule)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Paper mode — disable order placement regardless of .env setting",
    )

    args = parser.parse_args()

    try:
        config, logger = setup_environment(paper_mode=args.paper)

        if args.once:
            # Run immediately
            logger.info("Running strategy once (--once mode)")
            run_strategy(config, logger)
        else:
            # Run on daily schedule
            import schedule

            entry_time = config.strategy.entry_time  # e.g., "17:00"
            logger.info(f"Scheduling strategy to run daily at {entry_time} IST")

            def scheduled_run():
                """Wrapper for scheduled execution."""
                logger.info(f"Scheduled run triggered at {datetime.now(IST).strftime('%H:%M:%S IST')}")
                try:
                    run_strategy(config, logger)
                except Exception as e:
                    logger.error(f"Scheduled strategy run failed: {e}", exc_info=True)

            # Schedule uses local system time — since system is IST, this works directly
            schedule.every().day.at(entry_time).do(scheduled_run)

            logger.info(
                f"Scheduler active — waiting for {entry_time} IST daily. "
                f"Press Ctrl+C to stop."
            )

            while True:
                schedule.run_pending()
                time.sleep(1)

    except DeltaExchangeError as e:
        if 'logger' in locals():
            logger.error("Application error", error=str(e))
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        if 'logger' in locals():
            logger.info("Application interrupted by user")
        print("\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        if 'logger' in locals():
            logger.exception("Unexpected error", error=str(e))
        print(f"Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
