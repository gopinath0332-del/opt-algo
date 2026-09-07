"""Short Strangle strategy for Delta Exchange Options.

Strategy:
- Enters daily (e.g. 13:00 IST), selling OTM Call + OTM Put for BTC
- Uses otm_steps (e.g. otm_steps=2: ATM±2 strikes / ±$400)
- Pre-entry Reverse Momentum filter: Enters ONLY on high-momentum days (|move| > threshold_pct)
- Holds position to expiry settlement (17:30 IST), no stop-loss
- Dynamic lot sizing based on capital allocation (e.g. 30%)
"""

import math
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from api.rest_client import DeltaRestClient
from core.config import Config, StrategyConfig
from core.exceptions import APIError, TradingError
from core.firestore_client import journal_straddle_entry, journal_straddle_exit
from core.logger import get_logger
from notifications.manager import NotificationManager
from strategy.short_straddle import ShortStraddleStrategy, IST

logger = get_logger(__name__)


class ShortStrangleStrategy(ShortStraddleStrategy):
    """OTM Short Strangle strategy with reverse momentum filter."""

    def __init__(
        self,
        config: Config,
        client: DeltaRestClient,
        notifier: NotificationManager,
        strategy_config: Optional[StrategyConfig] = None,
    ):
        """Initialize Short Strangle strategy."""
        super().__init__(config, client, notifier, strategy_config=strategy_config)
        self.otm_steps = getattr(self.strategy_config, "otm_steps", 2)
        self.strategy_display_name = f"Short Strangle OTM+{self.otm_steps}"
        self.strategy_type_name = "short_strangle"

    def _check_momentum_filter(self) -> bool:
        """Check pre-entry reverse momentum filter.

        Fetches the underlying's spot price from `lookback_hours` ago
        and compares it with the current spot price.
        Enters trade ONLY if the absolute percentage move exceeds `threshold_pct`.

        Returns:
            True if trade should be SKIPPED, False if safe to proceed.
        """
        mf = self.strategy_config.momentum_filter
        if mf is None or not mf.enabled:
            return False

        lookback_hours = mf.lookback_hours
        threshold_pct = mf.threshold_pct
        symbol = f"{self.underlying}USD"

        logger.info(
            f"Reverse Momentum filter: checking {symbol} price change over "
            f"the last {lookback_hours}h (threshold: >{threshold_pct}%)"
        )

        try:
            now_ts = int(time.time())
            lookback_ts = int(now_ts - lookback_hours * 3600)

            candles = self.client.get_candles(
                symbol=symbol,
                resolution=60,
                start=lookback_ts - 3600,
                end=now_ts,
            )

            if not candles:
                logger.warning(
                    "Reverse Momentum filter: no candle data returned — "
                    "skipping trade (requires confirmed momentum)"
                )
                return True

            candles = sorted(candles, key=lambda x: x.get("time", 0))
            oldest_candle = candles[0]
            lookback_price = float(oldest_candle.get("open", 0))

            latest_candle = candles[-1]
            current_price = float(latest_candle.get("close", 0))

            if lookback_price <= 0 or current_price <= 0:
                logger.warning(
                    f"Reverse Momentum filter: invalid prices "
                    f"(lookback={lookback_price}, current={current_price}) — "
                    f"skipping trade (requires confirmed momentum)"
                )
                return True

            move_pct = abs(current_price - lookback_price) / lookback_price * 100
            direction = "UP" if current_price > lookback_price else "DOWN"

            logger.info(
                f"Reverse Momentum filter: {symbol} moved {move_pct:.2f}% in the last "
                f"{lookback_hours}h (lookback=${lookback_price:,.2f} → "
                f"current=${current_price:,.2f}, required: >{threshold_pct}%)"
            )

            if move_pct <= threshold_pct:
                logger.warning(
                    f"⚠️ Reverse Momentum filter TRIGGERED — {symbol} moved "
                    f"only {move_pct:.2f}% {direction} (<= {threshold_pct}%). "
                    f"Skipping trade entry."
                )
                self.notifier.send_status_message(
                    f"⚠️ Reverse Momentum Filter — Trade Skipped ({self.underlying})",
                    f"{symbol} moved only **{move_pct:.2f}% {direction}** "
                    f"in the {lookback_hours}h before entry.\n"
                    f"Lookback price: **${lookback_price:,.2f}**\n"
                    f"Current price: **${current_price:,.2f}**\n"
                    f"Required threshold: **>{threshold_pct}%**\n\n"
                    f"Trade entry skipped (requires strong directional momentum).",
                    color=15105570,  # Orange
                )
                return True

            logger.info(
                f"✅ Reverse Momentum filter PASSED — {symbol} moved "
                f"{move_pct:.2f}% {direction} (>{threshold_pct}%). "
                f"Proceeding with strangle entry."
            )
            return False

        except Exception as e:
            logger.warning(
                f"Reverse Momentum filter error: {e} — "
                f"skipping trade (requires confirmed momentum)",
                exc_info=True,
            )
            return True

    def run(self, resume_state: Optional[Dict[str, Any]] = None) -> None:
        """Execute full strangle strategy cycle: entry → wait → exit."""
        now = datetime.now(IST)
        logger.info(
            f"{self.strategy_display_name} strategy starting",
            underlying=self.underlying,
            mode=self.mode,
            time=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            order_placement=self.order_placement_enabled,
            resume=resume_state is not None,
        )

        # Weekend filter check
        if not resume_state and self.skip_weekends and now.weekday() in (5, 6):
            logger.info(f"Today is {now.strftime('%A')} (weekend). Skipping trade execution per configuration.")
            self.notifier.send_status_message(
                f"ℹ️ Weekend Skip — {self.underlying} ({self.strategy_display_name})",
                f"Today is {now.strftime('%A')} ({now.strftime('%Y-%m-%d')}). "
                f"Strategy is configured to skip weekend trading."
            )
            return

        try:
            if resume_state:
                logger.info("=" * 60)
                logger.info(f"RESUMING STRATEGY — Restoring state from active {self.strategy_display_name}")
                logger.info("=" * 60)
                self.trade_id = resume_state["trade_id"]
                self.call_product_id = int(resume_state["call_product_id"])
                self.put_product_id = int(resume_state["put_product_id"])
                self.call_symbol = resume_state["call_symbol"]
                self.put_symbol = resume_state["put_symbol"]
                self.lot_size = int(resume_state["lot_size"])
                self.entry_premium = float(resume_state["entry_premium"])
                self.call_entry_premium = float(resume_state.get("call_entry_premium", 0.0))
                self.put_entry_premium = float(resume_state.get("put_entry_premium", 0.0))
                self.sl_threshold = resume_state.get("sl_threshold")
                if self.sl_threshold is None and self.sl_pct is not None:
                    self.sl_threshold = self.entry_premium * self.sl_pct
                elif self.sl_threshold is None:
                    self.sl_threshold = float('inf')
                self.atm_strike = float(resume_state.get("atm_strike", 0.0))
                self.spot_price = float(resume_state.get("spot_price", 0.0))
                self.entry_time_us = int(resume_state.get("entry_time_us", int(time.time() * 1_000_000)))
                self.is_position_open = True

                sl_threshold_str = f"${self.sl_threshold:.4f}" if self.sl_threshold != float('inf') else "None (Disabled)"
                self.notifier.send_status_message(
                    f"🔄 Options Bot Resumed — {self.underlying} ({self.strategy_display_name})",
                    f"Resumed monitoring active {self.strategy_display_name}:\n"
                    f"ATM Strike: **{self.atm_strike}**\n"
                    f"Call: `{self.call_symbol}`\n"
                    f"Put: `{self.put_symbol}`\n"
                    f"Lot Size: **{self.lot_size}** per leg\n"
                    f"Total Premium collected: **${self.entry_premium:.4f}**\n"
                    f"SL Threshold: **{sl_threshold_str}**",
                    color=3447003,
                )
            else:
                # Step 0: Pre-entry reverse momentum filter
                if self._check_momentum_filter():
                    logger.info("Trade skipped by reverse momentum filter — ending strategy cycle.")
                    return

                # Step 1: Entry
                self._execute_entry()

            if not self.is_position_open:
                logger.warning("Entry failed — no positions opened. Aborting strategy cycle.")
                return

            if self.sl_pct is not None:
                # Step 2: Monitor for SL if configured
                sl_hit = self._monitor_stop_loss()
                if not sl_hit:
                    self._execute_exit(reason="scheduled_exit")
            else:
                # No SL — hold to exit time
                self._wait_until_exit_time()
                self._execute_exit(reason="scheduled_exit")

        except Exception as e:
            logger.error(f"{self.strategy_display_name} execution failed: {e}", exc_info=True)
            self.notifier.send_error(
                "Strategy Error",
                f"{self.strategy_display_name} failed: {e}"
            )
            self._emergency_exit()

    def _execute_entry(self) -> None:
        """Execute strangle entry: find OTM options and sell both legs."""
        logger.info("=" * 60)
        logger.info(f"STEP 1: ENTRY — Finding OTM+{self.otm_steps} options and selling strangle")
        logger.info("=" * 60)

        now = datetime.now(IST)
        self.trade_id = f"strangle_{self.underlying}_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        self.spot_price = self.client.get_spot_price(self.underlying)
        self.call_product, self.put_product, self.atm_strike = self.client.find_atm_options(
            underlying=self.underlying,
            spot_price=self.spot_price,
            otm_steps=self.otm_steps,
        )

        self.call_product_id = int(self.call_product["id"])
        self.put_product_id = int(self.put_product["id"])
        self.call_symbol = self.call_product.get("symbol", "UNKNOWN")
        self.put_symbol = self.put_product.get("symbol", "UNKNOWN")
        self.contract_value = float(self.call_product.get("contract_value", 0.001))

        logger.info(
            f"ATM Benchmark: {self.atm_strike} | "
            f"Call: {self.call_symbol} (ID: {self.call_product_id}) | "
            f"Put: {self.put_symbol} (ID: {self.put_product_id}) | "
            f"Contract Value: {self.contract_value}"
        )

        self.call_entry_premium = float(self.call_product.get("mark_price", 0))
        self.put_entry_premium = float(self.put_product.get("mark_price", 0))

        if self.call_entry_premium == 0 or self.put_entry_premium == 0:
            logger.warning("Mark price is 0 for one or both options — fetching from ticker")
            try:
                call_ticker = self.client.get_ticker(self.call_symbol)
                self.call_entry_premium = float(call_ticker.get("mark_price", 0))
            except Exception:
                pass
            try:
                put_ticker = self.client.get_ticker(self.put_symbol)
                self.put_entry_premium = float(put_ticker.get("mark_price", 0))
            except Exception:
                pass

        self.entry_premium = self.call_entry_premium + self.put_entry_premium
        self.call_entry_mark = self.call_entry_premium
        self.put_entry_mark = self.put_entry_premium
        self.entry_slippage_usd = 0.0

        # Dynamic lot sizing based on capital_allocation_pct
        self.available_balance: Optional[float] = None
        if self.static_lot_size is not None:
            self.lot_size = self.static_lot_size
            logger.info(f"Using static lot size from config: {self.lot_size} lots per leg")
        else:
            try:
                self.available_balance = self.client.get_available_balance()
                logger.info(f"Available wallet balance: ${self.available_balance:,.2f} USD")

                deployable_capital = self.available_balance * self.capital_allocation_pct
                margin_per_leg_per_lot = self._calculate_margin_per_leg(self.spot_price)
                margin_per_lot_both_legs = 2 * margin_per_leg_per_lot

                if margin_per_lot_both_legs > 0:
                    raw_lots = deployable_capital / margin_per_lot_both_legs
                    calculated_lots = math.floor(raw_lots)
                    if self.max_lot_size is not None:
                        calculated_lots = min(calculated_lots, self.max_lot_size)
                    self.lot_size = max(1, calculated_lots)
                else:
                    logger.warning("Calculated margin per lot is 0 — falling back to 1 lot")
                    self.lot_size = 1

                est_margin_deployed = self.lot_size * margin_per_lot_both_legs
                logger.info(
                    f"Dynamic lot sizing complete: "
                    f"Allocated: {self.capital_allocation_pct*100:.0f}% (${deployable_capital:,.2f}) | "
                    f"Lot size: {self.lot_size} lots/leg | "
                    f"Est. Margin: ${est_margin_deployed:,.2f}"
                )
            except Exception as e:
                logger.error(f"Failed to calculate dynamic lot size: {e} — falling back to 1 lot", exc_info=True)
                self.lot_size = 1

        sl_threshold_str = (
            f"${self.entry_premium * self.sl_pct:.4f}"
            if self.sl_pct is not None
            else "None (Disabled)"
        )

        logger.info(
            f"Entry premiums — Call: ${self.call_entry_premium:.4f}, "
            f"Put: ${self.put_entry_premium:.4f}, "
            f"Total: ${self.entry_premium:.4f}, "
            f"SL Threshold: {sl_threshold_str}"
        )

        if not self.order_placement_enabled:
            logger.warning(
                "[DISABLED] Order placement is disabled (ENABLE_ORDER_PLACEMENT=false). "
                "Logging signal only — no orders placed."
            )
            self.is_position_open = True

            margin_usd = 2 * self._calculate_margin_per_leg(self.spot_price) * self.lot_size
            self.notifier.send_entry_alert(
                underlying=self.underlying,
                strategy_name=self.strategy_display_name,
                spot_price=self.spot_price,
                atm_strike=self.atm_strike,
                call_symbol=self.call_symbol,
                put_symbol=self.put_symbol,
                call_premium=self.call_entry_premium,
                put_premium=self.put_entry_premium,
                total_premium=self.entry_premium,
                lot_size=self.lot_size,
                account_balance=self.available_balance,
                sl_threshold=self.sl_threshold,
                mode=self.mode,
                entry_slippage_usd=self.entry_slippage_usd,
                margin_usd=margin_usd,
            )
            return

        self.entry_time_us = int(time.time() * 1_000_000)

        # Set leverage
        if self.leverage:
            try:
                self.client.set_leverage(self.call_product_id, str(self.leverage))
                logger.info(f"Leverage set to {self.leverage}x for Call {self.call_symbol}")
            except Exception as e:
                logger.warning(f"Failed to set leverage for Call: {e}")

            try:
                self.client.set_leverage(self.put_product_id, str(self.leverage))
                logger.info(f"Leverage set to {self.leverage}x for Put {self.put_symbol}")
            except Exception as e:
                logger.warning(f"Failed to set leverage for Put: {e}")

        # Place orders
        call_order = None
        put_order = None

        try:
            logger.info(f"Selling {self.lot_size} lots of Call: {self.call_symbol}")
            call_order = self.client.place_order(
                product_id=self.call_product_id,
                size=self.lot_size,
                side="sell",
                order_type=self.order_type,
            )
            logger.info(f"Call order placed: {call_order.get('id')}")
        except Exception as e:
            err_str = str(e)
            alert_title = (
                "Market Disrupted — Call Order Failed"
                if "market_disrupted_cancel_only_mode" in err_str.lower()
                else "Call Order Failed"
            )
            logger.error(f"Failed to place Call order: {e}")
            self.notifier.send_error(alert_title, f"Short Strangle Call order failed: {e}")
            raise TradingError(f"Entry failed on Call leg: {e}")

        try:
            logger.info(f"Selling {self.lot_size} lots of Put: {self.put_symbol}")
            put_order = self.client.place_order(
                product_id=self.put_product_id,
                size=self.lot_size,
                side="sell",
                order_type=self.order_type,
            )
            logger.info(f"Put order placed: {put_order.get('id')}")
        except Exception as e:
            logger.error(f"Failed to place Put order: {e}")
            self.notifier.send_error("Put Order Failed", f"Short Strangle Put order failed: {e}")
            if call_order and call_order.get("id"):
                try:
                    self.client.cancel_order(self.call_product_id, str(call_order.get("id")))
                except Exception as ce:
                    logger.error(f"Failed to cancel orphaned Call order: {ce}")
            raise TradingError(f"Entry failed on Put leg: {e}")

        self.is_position_open = True

        # Fills and slippage polling
        call_entry_fill = None
        put_entry_fill = None
        call_order_id = call_order.get("id") if call_order else None
        put_order_id = put_order.get("id") if put_order else None

        time.sleep(2)
        if call_order_id:
            fills = self.client.get_fills(product_id=self.call_product_id, order_id=int(call_order_id))
            if fills:
                total_sz = sum(float(f.get("size", 0)) for f in fills)
                if total_sz > 0:
                    call_entry_fill = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills) / total_sz

        if put_order_id:
            fills = self.client.get_fills(product_id=self.put_product_id, order_id=int(put_order_id))
            if fills:
                total_sz = sum(float(f.get("size", 0)) for f in fills)
                if total_sz > 0:
                    put_entry_fill = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills) / total_sz

        call_diff = 0.0
        if call_entry_fill is not None:
            call_diff = (self.call_entry_mark - call_entry_fill) * self.lot_size * self.contract_value
            self.call_entry_premium = call_entry_fill

        put_diff = 0.0
        if put_entry_fill is not None:
            put_diff = (self.put_entry_mark - put_entry_fill) * self.lot_size * self.contract_value
            self.put_entry_premium = put_entry_fill

        self.entry_slippage_usd = call_diff + put_diff
        self.entry_premium = self.call_entry_premium + self.put_entry_premium
        self.sl_threshold = self.entry_premium * self.sl_pct if self.sl_pct is not None else None

        # Send entry alert
        margin_usd = 2 * self._calculate_margin_per_leg(self.spot_price) * self.lot_size
        self.notifier.send_entry_alert(
            underlying=self.underlying,
            strategy_name=self.strategy_display_name,
            spot_price=self.spot_price,
            atm_strike=self.atm_strike,
            call_symbol=self.call_symbol,
            put_symbol=self.put_symbol,
            call_premium=self.call_entry_premium,
            put_premium=self.put_entry_premium,
            total_premium=self.entry_premium,
            lot_size=self.lot_size,
            account_balance=self.available_balance,
            sl_threshold=self.sl_threshold,
            mode=self.mode,
            entry_slippage_usd=self.entry_slippage_usd,
            margin_usd=margin_usd,
        )

        # Journal to Firestore
        journal_straddle_entry(
            trade_id=self.trade_id,
            underlying=self.underlying,
            strategy_name=self.strategy_type_name,
            mode=self.mode,
            spot_price=self.spot_price,
            atm_strike=self.atm_strike,
            call_product_id=self.call_product_id,
            put_product_id=self.put_product_id,
            call_symbol=self.call_symbol,
            put_symbol=self.put_symbol,
            call_order_id=str(call_order.get("id")) if call_order else None,
            put_order_id=str(put_order.get("id")) if put_order else None,
            call_premium=self.call_entry_premium,
            put_premium=self.put_entry_premium,
            total_premium=self.entry_premium,
            lot_size=self.lot_size,
            leverage=self.leverage,
            entry_time=datetime.now(IST).isoformat(),
            entry_premium_points=self.entry_premium,
            total_premium_collected_usd=self.entry_premium * self.lot_size * self.contract_value,
            contract_value=self.contract_value,
            entry_slippage_usd=self.entry_slippage_usd,
        )

        logger.info(f"✅ {self.strategy_display_name} entry complete")

    def _execute_exit(self, reason: str = "scheduled_exit") -> None:
        """Close strangle and send exit alert with strangle branding."""
        logger.info("=" * 60)
        logger.info(f"STEP 3: EXIT — Closing {self.strategy_display_name} (reason: {reason})")
        logger.info("=" * 60)

        if not self.is_position_open:
            logger.info("No open positions to close")
            return

        let_settle = (reason == "scheduled_exit" and self.strategy_config.exit_time in ["17:30", "21:30"])

        exit_call_premium = 0.0
        exit_put_premium = 0.0
        exit_call_mark = 0.0
        exit_put_mark = 0.0

        if not let_settle:
            exit_call_premium = self._get_current_premium(self.call_product_id, self.call_symbol)
            exit_put_premium = self._get_current_premium(self.put_product_id, self.put_symbol)
            exit_call_mark = exit_call_premium
            exit_put_mark = exit_put_premium

        call_exit_order_id = None
        put_exit_order_id = None

        if self.order_placement_enabled and not let_settle:
            try:
                logger.info(f"Closing Call position: {self.call_symbol}")
                call_response = self.client.close_position(self.call_product_id)
                call_exit_order_id = str(call_response.get("id"))
            except Exception as e:
                logger.error(f"Failed to close Call position: {e}")
                self.notifier.send_error("Call Exit Failed", str(e))

            try:
                logger.info(f"Closing Put position: {self.put_symbol}")
                put_response = self.client.close_position(self.put_product_id)
                put_exit_order_id = str(put_response.get("id"))
            except Exception as e:
                logger.error(f"Failed to close Put position: {e}")
                self.notifier.send_error("Put Exit Failed", str(e))

            time.sleep(2)
            call_exit_fill = None
            if call_exit_order_id:
                fills = self.client.get_fills(product_id=self.call_product_id, order_id=int(call_exit_order_id))
                if fills:
                    total_sz = sum(float(f.get("size", 0)) for f in fills)
                    if total_sz > 0:
                        call_exit_fill = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills) / total_sz
            if call_exit_fill is not None:
                exit_call_premium = call_exit_fill

            put_exit_fill = None
            if put_exit_order_id:
                fills = self.client.get_fills(product_id=self.put_product_id, order_id=int(put_exit_order_id))
                if fills:
                    total_sz = sum(float(f.get("size", 0)) for f in fills)
                    if total_sz > 0:
                        put_exit_fill = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills) / total_sz
            if put_exit_fill is not None:
                exit_put_premium = put_exit_fill

        elif let_settle:
            logger.info(f"Holding {self.strategy_display_name} to expiry (auto-settlement).")
            time.sleep(20)

        # Calculate PnL and ledger cross-checks
        exit_total = exit_call_premium + exit_put_premium
        pnl_points = self.entry_premium - exit_total

        # Delta Exchange transaction queries
        call_realized_pnl: Optional[float] = None
        put_realized_pnl: Optional[float] = None
        call_comm: Optional[float] = None
        put_comm: Optional[float] = None

        if self.order_placement_enabled:
            time.sleep(5)
            try:
                tx_list = self.client.get_wallet_transactions(
                    start_time=self.entry_time_us,
                    end_time=int(time.time() * 1_000_000),
                )
                if isinstance(tx_list, list):
                    call_id_str = str(self.call_product_id)
                    put_id_str = str(self.put_product_id)
                    for tx in tx_list:
                        p_id = str(tx.get("product_id", ""))
                        tx_type = tx.get("transaction_type", "")
                        amt = float(tx.get("amount", 0.0))
                        if p_id == call_id_str:
                            if tx_type == "pnl":
                                call_realized_pnl = (call_realized_pnl or 0.0) + amt
                            elif tx_type == "commission":
                                call_comm = (call_comm or 0.0) + abs(amt)
                        elif p_id == put_id_str:
                            if tx_type == "pnl":
                                put_realized_pnl = (put_realized_pnl or 0.0) + amt
                            elif tx_type == "commission":
                                put_comm = (put_comm or 0.0) + abs(amt)
            except Exception as e:
                logger.warning(f"Failed to fetch wallet transactions: {e}")

        is_exchange_sourced = (call_realized_pnl is not None and put_realized_pnl is not None)
        calculated_pnl_usd = (self.entry_premium - exit_total) * self.lot_size * self.contract_value
        final_pnl_usd = (call_realized_pnl + put_realized_pnl) if is_exchange_sourced else calculated_pnl_usd

        call_pnl_usd = call_realized_pnl if call_realized_pnl is not None else (self.call_entry_premium - exit_call_premium) * self.lot_size * self.contract_value
        put_pnl_usd = put_realized_pnl if put_realized_pnl is not None else (self.put_entry_premium - exit_put_premium) * self.lot_size * self.contract_value

        taker_fee_pct = 0.0005
        call_fee_est = (self.call_entry_premium + exit_call_premium) * self.lot_size * self.contract_value * taker_fee_pct
        put_fee_est = (self.put_entry_premium + exit_put_premium) * self.lot_size * self.contract_value * taker_fee_pct
        if call_comm is None:
            call_comm = call_fee_est
        if put_comm is None:
            put_comm = put_fee_est
        trading_fees = (call_comm or 0.0) + (put_comm or 0.0)
        gross_pnl_usd = final_pnl_usd
        net_pnl_usd = gross_pnl_usd - trading_fees

        exit_diff = 0.0
        if not let_settle:
            exit_diff = ((exit_call_premium - exit_call_mark) + (exit_put_premium - exit_put_mark)) * self.lot_size * self.contract_value
        exit_slippage_usd = exit_diff
        total_slippage_usd = self.entry_slippage_usd + exit_slippage_usd

        self.is_position_open = False

        self.notifier.send_exit_alert(
            underlying=self.underlying,
            strategy_name=self.strategy_display_name,
            exit_reason=reason,
            entry_premium=self.entry_premium,
            exit_premium=exit_total,
            realized_pnl=final_pnl_usd,
            call_symbol=self.call_symbol,
            put_symbol=self.put_symbol,
            exit_call_premium=exit_call_premium,
            exit_put_premium=exit_put_premium,
            mode=self.mode,
            exit_slippage_usd=exit_slippage_usd,
            total_slippage_usd=total_slippage_usd,
            exchange_realized_pnl=calculated_pnl_usd if is_exchange_sourced else None,
            is_exchange_sourced=is_exchange_sourced,
            call_entry_premium=self.call_entry_premium,
            put_entry_premium=self.put_entry_premium,
            call_pnl=call_pnl_usd,
            put_pnl=put_pnl_usd,
            call_fee=call_comm,
            put_fee=put_comm,
            total_fee=trading_fees,
            gross_pnl=gross_pnl_usd,
            net_pnl=net_pnl_usd,
        )

        journal_straddle_exit(
            trade_id=self.trade_id,
            exit_reason=reason,
            exit_call_premium=exit_call_premium,
            exit_put_premium=exit_put_premium,
            realized_pnl=final_pnl_usd,
            max_mtm_loss=self.max_mtm_loss * self.lot_size * self.contract_value,
            call_exit_order_id=call_exit_order_id,
            put_exit_order_id=put_exit_order_id,
            pnl_points=pnl_points,
            trading_fees=trading_fees,
            contract_value=self.contract_value,
            exit_slippage_usd=exit_slippage_usd,
            total_slippage_usd=total_slippage_usd,
        )

        logger.info(f"✅ {self.strategy_display_name} exit complete")
