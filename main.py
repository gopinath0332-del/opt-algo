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

    lot_size_str = (
        f"{config.strategy.lot_size} (static)"
        if config.strategy.lot_size
        else f"Dynamic ({config.strategy.capital_allocation_pct:.0f}% of balance)"
    )
    startup_msg = (
        f"Strategy: \u001b[1;37mShort Straddle\u001b[0m\n"
        f"Underlying: \u001b[1;37m{config.strategy.underlying}\u001b[0m\n"
        f"Environment: \u001b[1;37m{config.environment}\u001b[0m\n"
        f"Mode: \u001b[1;37m{mode}\u001b[0m\n"
        f"Order Placement: \u001b[{'1;32' if config.enable_order_placement else '0;31'}m"
        f"{'ENABLED' if config.enable_order_placement else 'DISABLED'}\u001b[0m\n"
        f"Lot Size: \u001b[0;36m{lot_size_str}\u001b[0m per leg\n"
        f"Leverage: \u001b[0;35m{config.strategy.leverage}x\u001b[0m\n"
        f"SL: \u001b[0;33m{f'{config.strategy.stop_loss.value}% of premium' if config.strategy.stop_loss else 'None (Disabled)'}\u001b[0m\n"
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


def check_and_resume_active_trades(config, logger):
    """Check if the service was restarted during an active trade window and resume monitoring."""
    now = datetime.now(IST)
    entry_time = config.strategy.entry_time
    exit_time = config.strategy.exit_time
    
    try:
        entry_h, entry_m = map(int, entry_time.split(":"))
        exit_h, exit_m = map(int, exit_time.split(":"))
    except ValueError as e:
        logger.error(f"Invalid entry/exit time format in configuration: {e}")
        return

    today_entry = now.replace(hour=entry_h, minute=entry_m, second=0, microsecond=0)
    today_exit = now.replace(hour=exit_h, minute=exit_m, second=0, microsecond=0)

    if today_entry <= now < today_exit:
        logger.info("Service started/restarted within the trading window. Performing recovery checks...")
        try:
            recovered_state = None

            # 1. Primary Recovery: Firestore
            if config.firestore_enabled:
                from core.firestore_client import get_open_trades
                open_trades = get_open_trades(config.strategy.underlying)
                if open_trades:
                    # Look for active trades on the exchange corresponding to Firestore open records
                    from api.rest_client import DeltaRestClient
                    client = DeltaRestClient(config)
                    try:
                        positions = client.get_positions()
                    except Exception as e:
                        logger.error(f"Failed to fetch positions from exchange during recovery: {e}")
                        positions = []

                    for trade in open_trades:
                        call_pid = trade.get("call_product_id")
                        put_pid = trade.get("put_product_id")
                        if call_pid and put_pid:
                            has_call = any(str(p.get("product_id")) == str(call_pid) and float(p.get("size", 0)) < 0 for p in positions)
                            has_put = any(str(p.get("product_id")) == str(put_pid) and float(p.get("size", 0)) < 0 for p in positions)

                            if has_call and has_put:
                                logger.info(f"Found active open trade in Firestore (ID: {trade.get('trade_id')}) with matching open positions on Delta Exchange.")
                                recovered_state = {
                                    "trade_id": trade.get("trade_id"),
                                    "call_product_id": call_pid,
                                    "put_product_id": put_pid,
                                    "call_symbol": trade.get("call_symbol"),
                                    "put_symbol": trade.get("put_symbol"),
                                    "lot_size": trade.get("lot_size"),
                                    "entry_premium": trade.get("total_premium_collected"),
                                    "call_entry_premium": trade.get("call_premium"),
                                    "put_entry_premium": trade.get("put_premium"),
                                    "sl_threshold": trade.get("sl_threshold"),
                                    "atm_strike": trade.get("atm_strike"),
                                    "spot_price": trade.get("spot_price"),
                                    "entry_time_us": int(datetime.fromisoformat(trade.get("entry_time_ist")).timestamp() * 1_000_000) if trade.get("entry_time_ist") else int(time.time() * 1_000_000)
                                }
                                break
                            else:
                                logger.warning(f"Trade {trade.get('trade_id')} is marked OPEN in Firestore but matching positions are not active on Delta Exchange.")

            # 2. Fallback Recovery: Delta Exchange Positions directly
            if not recovered_state:
                logger.info("Firestore recovery did not find any active trade. Checking Delta Exchange positions directly...")
                from api.rest_client import DeltaRestClient
                client = DeltaRestClient(config)
                try:
                    positions = client.get_positions()
                except Exception as e:
                    logger.error(f"Failed to fetch positions from exchange: {e}")
                    positions = []

                short_options = []
                for pos in positions:
                    size = float(pos.get("size", 0))
                    if size < 0:  # Short position
                        symbol = pos.get("product_symbol", "")
                        pid = pos.get("product_id")
                        if (symbol.startswith("C-") or symbol.startswith("P-")) and config.strategy.underlying in symbol:
                            short_options.append({
                                "product_id": pid,
                                "symbol": symbol,
                                "size": abs(int(size)),
                                "entry_price": float(pos.get("entry_price", 0.0))
                            })

                groups = {}
                for opt in short_options:
                    suffix = opt["symbol"][2:]
                    if suffix not in groups:
                        groups[suffix] = []
                    groups[suffix].append(opt)

                for suffix, opts in groups.items():
                    if len(opts) >= 2:
                        calls = [o for o in opts if o["symbol"].startswith("C-")]
                        puts = [o for o in opts if o["symbol"].startswith("P-")]
                        if calls and puts:
                            call_opt = calls[0]
                            put_opt = puts[0]

                            if call_opt["size"] == put_opt["size"]:
                                logger.info(f"Recovered open straddle from exchange: Call={call_opt['symbol']}, Put={put_opt['symbol']}, size={call_opt['size']}")
                                parts = suffix.split("-")
                                strike = 0.0
                                if len(parts) >= 2:
                                    try:
                                        strike = float(parts[1])
                                    except ValueError:
                                        pass

                                entry_prem = call_opt["entry_price"] + put_opt["entry_price"]
                                trade_id = f"recovered_straddle_{config.strategy.underlying}_{now.strftime('%Y%m%d_%H%M%S')}"

                                recovered_state = {
                                    "trade_id": trade_id,
                                    "call_product_id": call_opt["product_id"],
                                    "put_product_id": put_opt["product_id"],
                                    "call_symbol": call_opt["symbol"],
                                    "put_symbol": put_opt["symbol"],
                                    "lot_size": call_opt["size"],
                                    "entry_premium": entry_prem,
                                    "call_entry_premium": call_opt["entry_price"],
                                    "put_entry_premium": put_opt["entry_price"],
                                    "sl_threshold": entry_prem * (config.strategy.stop_loss.value / 100.0) if config.strategy.stop_loss else None,
                                    "atm_strike": strike,
                                    "spot_price": strike,
                                    "entry_time_us": int(time.time() * 1_000_000)
                                }
                                break

            # 3. Resume strategy execution if trade state was recovered
            if recovered_state:
                logger.info(f"Resuming options trade monitoring for trade {recovered_state['trade_id']}")
                from api.rest_client import DeltaRestClient
                from notifications.manager import NotificationManager
                from strategy.short_straddle import ShortStraddleStrategy

                client = DeltaRestClient(config)
                notifier = NotificationManager(config)
                strategy = ShortStraddleStrategy(config, client, notifier)
                strategy.run(resume_state=recovered_state)
                logger.info("Resumed trade execution complete. Returning to daily scheduler.")
            else:
                logger.info("No active options trades detected to resume.")

        except Exception as ex:
            logger.error(f"Error during options bot recovery: {ex}", exc_info=True)


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

            from notifications.manager import NotificationManager
            notifier = NotificationManager(config)
            
            mode_str = "🟢 LIVE TRADING" if config.enable_order_placement else "🟡 PAPER TRADING"
            sl_str = f"{config.strategy.stop_loss.value}% of premium" if config.strategy.stop_loss else "None (Disabled)"
            startup_msg = (
                f"```ansi\n"
                f"Status: \u001b[{'1;32' if config.enable_order_placement else '1;33'}m{mode_str}\u001b[0m\n"
                f"Strategy: \u001b[1;37m{config.strategy.name}\u001b[0m\n"
                f"Underlying: \u001b[1;37m{config.strategy.underlying}\u001b[0m\n"
                f"Scheduled Entry: \u001b[0;36m{entry_time} IST\u001b[0m\n"
                f"Scheduled Exit: \u001b[0;36m{config.strategy.exit_time} IST\u001b[0m\n"
                f"Stop Loss: \u001b[0;33m{sl_str}\u001b[0m\n"
                f"System Time: \u001b[0;36m{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\u001b[0m\n"
                f"```\n"
                f"*Service is online and waiting for the scheduled execution time.*"
            )
            
            notifier.send_status_message(
                "🔄 Options Bot Service Started/Restarted",
                startup_msg,
                color=3447003,  # Blue
            )

            # Check and resume active trades on startup
            check_and_resume_active_trades(config, logger)

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
