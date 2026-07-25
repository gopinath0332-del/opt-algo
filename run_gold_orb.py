"""
run_gold_orb.py
===============
Dedicated live/paper runner for Gold ORB strategy on XAUTUSD.

Runs in parallel with short_straddle without modifying or interrupting
the existing options bot.

Usage:
    # Run in live trading mode:
    python run_gold_orb.py

    # Run in paper / signal mode (orders not sent):
    python run_gold_orb.py --paper
"""

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

# Add project root to sys.path
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from core.config import get_config
from core.firestore_client import initialize_firestore
from core.logger import get_logger, setup_logging
from api.rest_client import DeltaRestClient
from notifications.discord import DiscordNotifier
from strategy.gold_orb_strategy import GoldOrbStrategy

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "XAUTUSD"


def setup_gold_environment(paper_mode: bool = False):
    config = get_config()
    setup_logging(
        log_level=config.log_level,
        log_file="logs/gold_orb.log",
        log_max_bytes=config.log_max_bytes,
        log_backup_count=config.log_backup_count,
        discord_error_webhook_url=config.discord_error_webhook_url if config.discord_enabled else None,
        alert_throttle_seconds=config.alert_throttle_seconds,
        enable_error_alerts=config.enable_error_alerts,
    )
    logger = get_logger("GoldORBRunner")

    if paper_mode:
        config.enable_order_placement = False
        logger.info("Paper trading mode enabled for Gold ORB")

    initialize_firestore(
        service_account_path=config.firestore_service_account_path,
        collection_name=config.firestore_collection_name,
        enabled=config.firestore_enabled,
    )

    return config, logger


def get_xautusd_product_id(client: DeltaRestClient) -> int:
    """Fetch product ID for XAUTUSD futures contract."""
    try:
        products = client.get_products()
        for p in products:
            if p.get("symbol") == SYMBOL:
                return int(p["id"])
    except Exception as e:
        pass
    # Fallback to direct lookup
    res = client._make_direct_request("/v2/products")
    for p in res.get("result", []):
        if p.get("symbol") == SYMBOL:
            return int(p["id"])
    raise RuntimeError(f"Could not find product_id for {SYMBOL}")


def fetch_1h_candles_dataframe(client: DeltaRestClient) -> pd.DataFrame:
    """Fetch recent 1H candles from Delta Exchange and format as DataFrame."""
    candles = client.get_candles(SYMBOL, resolution=60, count=48)
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    # Delta candles return: time, open, high, low, close, volume
    df = df.sort_values("time").reset_index(drop=True)
    return df


def main():
    parser = argparse.ArgumentParser(description="Gold ORB Live Runner")
    parser.add_argument("--paper", action="store_true", help="Run in paper/signal mode (orders disabled)")
    args = parser.parse_args()

    config, logger = setup_gold_environment(paper_mode=args.paper)
    client = DeltaRestClient(config)

    webhook_url = getattr(config, "discord_webhook_url", None)
    notifier = DiscordNotifier(webhook_url) if webhook_url else None

    strategy = GoldOrbStrategy(SYMBOL)
    product_id = get_xautusd_product_id(client)
    logger.info(f"Resolved {SYMBOL} product_id: {product_id}")

    # Reconcile state on startup (crash recovery)
    strategy.reconcile_on_restart(client, product_id)

    mode_str = "🟢 LIVE TRADING" if config.enable_order_placement else "🟡 PAPER TRADING"
    startup_msg = (
        f"Strategy: \u001b[1;37mGold ORB (0.5% Breakout)\u001b[0m\n"
        f"Symbol: \u001b[1;37m{SYMBOL}\u001b[0m\n"
        f"Mode: \u001b[1;37m{mode_str}\u001b[0m\n"
        f"Fixed Lots: \u001b[0;36m{strategy.fixed_lot_size}\u001b[0m\n"
        f"Leverage: \u001b[0;35m{strategy.leverage}x\u001b[0m\n"
        f"Threshold: \u001b[0;33m{strategy.pct_threshold*100:.2f}%\u001b[0m\n"
        f"Risk/Reward: \u001b[0;32m1 : {strategy.rr_ratio}\u001b[0m\n"
        f"Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
    )

    if notifier:
        notifier.send_status_message(f"🏆 Gold ORB Bot Online — {SYMBOL}", startup_msg, color=5763719)

    logger.info("Gold ORB Strategy loop active. Polling on candle boundaries...")

    last_processed_candle_time = None

    while True:
        try:
            now_ist = datetime.now(IST)

            # Check for EOD session closure at 05:30 IST
            if now_ist.hour == 5 and now_ist.minute == 30 and strategy.current_position != 0:
                candles = fetch_1h_candles_dataframe(client)
                curr_p = float(candles.iloc[-1]["close"]) if not candles.empty else 0.0
                logger.info(f"[{SYMBOL}] EOD closure triggered at 05:30 IST")
                strategy.close_eod_position(client, product_id, curr_p)

            # Process 1H candles on candle close
            df = fetch_1h_candles_dataframe(client)

            if not df.empty:
                latest_candle_time = df.iloc[-1]["time"]

                if latest_candle_time != last_processed_candle_time:
                    last_processed_candle_time = latest_candle_time

                    # Reconcile position state with exchange fills
                    if strategy.current_position != 0:
                        try:
                            positions = client.get_positions(product_id=product_id)
                            pos = next((p for p in positions if str(p.get("product_id")) == str(product_id)), None)
                            active_size = float(pos.get("size", 0)) if pos else 0.0

                            if active_size == 0:
                                logger.info(f"[{SYMBOL}] Position closed on exchange (TP/SL hit). Syncing exit...")
                                exit_price = float(df.iloc[-1]["close"])
                                action = "EXIT_LONG" if strategy.current_position == 1 else "EXIT_SHORT"
                                strategy.update_position_state(action, price=exit_price, reason="TP/SL Hit on Exchange")
                        except Exception as e:
                            logger.error(f"Error checking position state: {e}")

                    # Evaluate signal
                    signal, reason = strategy.check_signals(df)

                    if signal in ("ENTRY_LONG", "ENTRY_SHORT"):
                        side = "buy" if signal == "ENTRY_LONG" else "sell"
                        entry_price = float(df.iloc[-1]["close"])

                        logger.info(f"[{SYMBOL}] Signal: {signal} @ ${entry_price:.2f} ({reason})")

                        if config.enable_order_placement:
                            # 1. Set leverage
                            client.set_leverage(product_id, strategy.leverage)
                            # 2. Place market entry order
                            order = client.place_order(
                                product_id=product_id,
                                size=strategy.fixed_lot_size,
                                side=side,
                                order_type="market_order",
                            )
                            logger.info(f"[{SYMBOL}] Entry order placed: {order.get('result')}")

                            # 3. Update strategy state
                            strategy.update_position_state(signal, price=entry_price, reason=reason)

                            # 4. Place OCO bracket order for TP and SL
                            client.place_bracket_order(
                                product_id=product_id,
                                product_symbol=SYMBOL,
                                tp_price=strategy.tp_price,
                                sl_price=strategy.sl_price,
                            )
                        else:
                            logger.info(f"[{SYMBOL}] [PAPER] Order simulated for {signal}")
                            strategy.update_position_state(signal, price=entry_price, reason=reason)

        except Exception as e:
            logger.error(f"[{SYMBOL}] Unexpected error in strategy loop: {e}", exc_info=True)

        time.sleep(15)  # Poll loop interval


if __name__ == "__main__":
    main()
