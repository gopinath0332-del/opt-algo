"""Short Straddle strategy for Delta Exchange Options.

Strategy:
- Every day at 17:00 IST, sell ATM Call + ATM Put for BTC
- Lot size: 250 contracts per leg
- Leverage: 200x
- Combined stop-loss: 50% of total entry premium collected
- Exit at 17:25 IST if SL not hit
- All orders are market orders
"""

import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from api.rest_client import DeltaRestClient
from core.config import Config
from core.exceptions import APIError, TradingError
from core.firestore_client import journal_straddle_entry, journal_straddle_exit
from core.logger import get_logger
from notifications.manager import NotificationManager

logger = get_logger(__name__)

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))


class ShortStraddleStrategy:
    """Short Straddle strategy — sell ATM Call + Put, monitor combined SL, exit on schedule."""

    def __init__(self, config: Config, client: DeltaRestClient, notifier: NotificationManager):
        """Initialize the strategy.

        Args:
            config: Application configuration
            client: Delta Exchange REST client
            notifier: Notification manager
        """
        self.config = config
        self.client = client
        self.notifier = notifier

        # Strategy parameters from config
        self.underlying = config.strategy.underlying
        self.static_lot_size = config.strategy.lot_size           # None = use dynamic sizing
        self.max_lot_size = config.strategy.max_lot_size           # None = no limit
        self.capital_allocation_pct = config.strategy.capital_allocation_pct / 100.0
        self.lot_size: int = config.strategy.lot_size or 1        # Will be overridden dynamically at entry
        self.leverage = config.strategy.leverage
        self.option_margin_requirement_pct = config.strategy.option_margin_requirement_pct / 100.0
        self.skip_weekends = config.strategy.skip_weekends
        self.sl_pct = config.strategy.stop_loss.value / 100.0 if config.strategy.stop_loss else None
        self.monitor_interval = config.strategy.monitor_interval_sec
        self.order_type = config.strategy.order_type

        # Trading mode
        self.mode = config.get_mode()  # 'live' or 'paper'
        self.order_placement_enabled = config.enable_order_placement

        # State tracking
        self.trade_id: Optional[str] = None
        self.call_product: Optional[Dict[str, Any]] = None
        self.put_product: Optional[Dict[str, Any]] = None
        self.call_product_id: Optional[int] = None
        self.put_product_id: Optional[int] = None
        self.call_symbol: Optional[str] = None
        self.put_symbol: Optional[str] = None
        self.entry_premium: float = 0.0
        self.call_entry_premium: float = 0.0
        self.put_entry_premium: float = 0.0
        self.sl_threshold: float = 0.0
        self.atm_strike: float = 0.0
        self.spot_price: float = 0.0
        self.is_position_open: bool = False
        self.max_mtm_loss: float = 0.0
        self.contract_value: float = 0.001
        self.entry_time_us: int = 0

    def _calculate_margin_per_leg(self, spot_price: float) -> float:
        """Calculate estimated margin per leg per lot."""
        if self.leverage and self.leverage > 0:
            return (spot_price * self.contract_value) / self.leverage
        return spot_price * self.contract_value * self.option_margin_requirement_pct

    def run(self, resume_state: Optional[Dict[str, Any]] = None) -> None:
        """Execute the full strategy cycle: entry → monitor → exit.

        If resume_state is provided, it skips entry and resumes monitoring directly.
        """
        now = datetime.now(IST)
        logger.info(
            f"Short Straddle strategy starting",
            underlying=self.underlying,
            mode=self.mode,
            time=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            order_placement=self.order_placement_enabled,
            resume=resume_state is not None,
        )

        # Check weekend filter (skip check if resuming)
        if not resume_state and self.skip_weekends and now.weekday() in (5, 6):
            logger.info(f"Today is {now.strftime('%A')} (weekend). Skipping trade execution per configuration.")
            self.notifier.send_status_message(
                f"ℹ️ Weekend Skip — {self.underlying}",
                f"Today is {now.strftime('%A')} ({now.strftime('%Y-%m-%d')}). "
                f"Strategy is configured to skip weekend trading."
            )
            return

        try:
            if resume_state:
                logger.info("=" * 60)
                logger.info("RESUMING STRATEGY — Restoring state from active trade")
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
                
                # Send Discord resumption notification
                sl_threshold_str = f"${self.sl_threshold:.4f}" if self.sl_threshold != float('inf') else "None (Disabled)"
                self.notifier.send_status_message(
                    f"🔄 Options Bot Resumed — {self.underlying}",
                    f"Resumed monitoring active short straddle:\n"
                    f"Strike: **{self.atm_strike}**\n"
                    f"Call: `{self.call_symbol}`\n"
                    f"Put: `{self.put_symbol}`\n"
                    f"Lot Size: **{self.lot_size}** per leg\n"
                    f"Total Premium collected: **${self.entry_premium:.4f}**\n"
                    f"SL Threshold: **{sl_threshold_str}**",
                    color=3447003, # Blue
                )
            else:
                # Step 1: Entry
                self._execute_entry()

            if not self.is_position_open:
                logger.warning("Entry failed — no positions opened. Aborting strategy cycle.")
                return

            # Step 2: Monitor for SL
            sl_hit = self._monitor_stop_loss()

            # Step 3: Exit (if SL was not hit, positions are still open)
            if not sl_hit:
                self._execute_exit(reason="scheduled_exit")

        except Exception as e:
            logger.error(f"Strategy execution failed: {e}", exc_info=True)
            self.notifier.send_error(
                "Strategy Error",
                f"Short Straddle failed: {e}"
            )
            # Attempt emergency exit
            self._emergency_exit()

    def _execute_entry(self) -> None:
        """Execute the straddle entry: find ATM options and sell both legs."""
        logger.info("=" * 60)
        logger.info("STEP 1: ENTRY — Finding ATM options and selling straddle")
        logger.info("=" * 60)

        # Generate unique trade ID
        now = datetime.now(IST)
        self.trade_id = f"straddle_{self.underlying}_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # Get spot price and find ATM options
        self.spot_price = self.client.get_spot_price(self.underlying)
        self.call_product, self.put_product, self.atm_strike = self.client.find_atm_options(
            underlying=self.underlying,
            spot_price=self.spot_price,
        )

        self.call_product_id = int(self.call_product["id"])
        self.put_product_id = int(self.put_product["id"])
        self.call_symbol = self.call_product.get("symbol", "UNKNOWN")
        self.put_symbol = self.put_product.get("symbol", "UNKNOWN")
        self.contract_value = float(self.call_product.get("contract_value", 0.001))

        logger.info(
            f"ATM Strike: {self.atm_strike} | "
            f"Call: {self.call_symbol} (ID: {self.call_product_id}) | "
            f"Put: {self.put_symbol} (ID: {self.put_product_id}) | "
            f"Contract Value: {self.contract_value}"
        )

        # Get entry premiums (mark prices before placing orders)
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

        # ---------------------------------------------------------------
        # Dynamic lot size calculation
        # ---------------------------------------------------------------
        self.available_balance: Optional[float] = None
        if self.static_lot_size is not None:
            # Static override — use config value directly
            self.lot_size = self.static_lot_size
            logger.info(f"Using static lot size from config: {self.lot_size} lots per leg")
        else:
            # Dynamic sizing: allocate capital_allocation_pct % of available balance
            #
            # IMPORTANT: sizing MUST be based on the isolated MARGIN required per lot,
            # NOT the premium collected. Delta Exchange holds margin based on notional,
            # not on the premium, so using premium leads to over-sizing and
            # "insufficient_margin" errors.
            #
            # For each leg (call + put) the exchange requires:
            #   margin_per_leg = spot_price × contract_value × option_margin_requirement_pct
            #
            # For a straddle (2 legs):
            #   total_margin_per_lot = 2 × margin_per_leg
            #
            # Lot size derivation:
            #   capital               = available_balance × capital_allocation_pct
            #   lot_size              = floor(capital / total_margin_per_lot)
            #
            self.available_balance = self.client.get_available_balance()
            available_balance = self.available_balance
            if available_balance > 0 and self.spot_price > 0 and self.contract_value > 0:
                capital = available_balance * self.capital_allocation_pct

                # ----------------------------------------------------------
                # Primary: ask the exchange for the actual margin per lot.
                # POST /v2/orders/compute_margin mirrors the exchange UI and
                # accounts for the premium offset that shorts receive.
                # We query 1-lot margin for the call (the heavier leg) and
                # double it for both legs, then binary-search to find the
                # exact max lots that fit inside `capital`.
                # ----------------------------------------------------------
                margin_per_lot: Optional[float] = None
                try:
                    call_margin_1lot = self.client.get_order_margin(
                        product_id=self.call_product_id,
                        size=1,
                        side="sell",
                        order_type=self.order_type,
                    )
                    put_margin_1lot = self.client.get_order_margin(
                        product_id=self.put_product_id,
                        size=1,
                        side="sell",
                        order_type=self.order_type,
                    )
                    if call_margin_1lot is not None and put_margin_1lot is not None:
                        margin_per_lot = call_margin_1lot + put_margin_1lot
                        logger.info(
                            f"Exchange margin (1 lot): "
                            f"Call=${call_margin_1lot:.4f}, "
                            f"Put=${put_margin_1lot:.4f}, "
                            f"Combined=${margin_per_lot:.4f}"
                        )
                except Exception as e:
                    logger.warning(f"compute_margin API failed, will use fallback formula: {e}")

                if margin_per_lot is not None and margin_per_lot > 0:
                    # Use exchange-reported margin per lot directly.
                    # Note: margin is *not* strictly linear at scale (portfolio
                    # margin offsets change), so we verify with the full lot
                    # count once calculated and step down if needed.
                    candidate_lots = max(1, int(capital / margin_per_lot))
                    if self.max_lot_size is not None:
                        candidate_lots = min(candidate_lots, self.max_lot_size)

                    # Verify the full-size order fits — step down if over budget
                    for lots in range(candidate_lots, 0, -1):
                        try:
                            call_margin_full = self.client.get_order_margin(
                                product_id=self.call_product_id,
                                size=lots,
                                side="sell",
                                order_type=self.order_type,
                            ) or 0.0
                            put_margin_full = self.client.get_order_margin(
                                product_id=self.put_product_id,
                                size=lots,
                                side="sell",
                                order_type=self.order_type,
                            ) or 0.0
                            total_margin_full = call_margin_full + put_margin_full
                            if total_margin_full <= capital:
                                self.lot_size = lots
                                logger.info(
                                    f"Dynamic lot size (exchange margin): "
                                    f"balance=${available_balance:,.2f}, "
                                    f"capital ({self.capital_allocation_pct*100:.0f}%)=${capital:,.2f}, "
                                    f"margin_for_{lots}_lots=${total_margin_full:.4f}, "
                                    f"lot_size={lots}"
                                )
                                break
                        except Exception:
                            # If verification call fails, accept the candidate
                            self.lot_size = candidate_lots
                            logger.info(
                                f"Dynamic lot size (exchange margin, unverified): lot_size={candidate_lots}"
                            )
                            break
                    else:
                        self.lot_size = 1
                        logger.warning("Exchange margin exceeds capital even for 1 lot — defaulting to 1")

                else:
                    # ----------------------------------------------------------
                    # Fallback: raw notional-based estimate (original formula).
                    # Used when compute_margin API is unavailable.
                    # ----------------------------------------------------------
                    margin_per_leg = self._calculate_margin_per_leg(self.spot_price)
                    total_margin_per_lot = 2 * margin_per_leg
                    if total_margin_per_lot > 0:
                        self.lot_size = max(1, int(capital / total_margin_per_lot))
                        if self.max_lot_size is not None:
                            self.lot_size = min(self.lot_size, self.max_lot_size)
                        
                        effective_margin_pct = (1.0 / self.leverage) if (self.leverage and self.leverage > 0) else self.option_margin_requirement_pct
                        logger.info(
                            f"Dynamic lot size (fallback formula): "
                            f"balance=${available_balance:,.2f}, "
                            f"capital ({self.capital_allocation_pct*100:.0f}%)=${capital:,.2f}, "
                            f"spot=${self.spot_price:,.2f}, "
                            f"contract_value={self.contract_value}, "
                            f"margin_pct={effective_margin_pct*100:.2f}%, "
                            f"margin/leg=${margin_per_leg:.4f}, "
                            f"total_margin/lot=${total_margin_per_lot:.4f}, "
                            f"lot_size={self.lot_size}"
                        )
                    else:
                        self.lot_size = 1
                        logger.warning("Margin per lot is 0 — defaulting lot size to 1")
            else:
                self.lot_size = 1
                logger.warning(
                    f"Could not compute dynamic lot size "
                    f"(balance=${available_balance:.2f}, spot=${self.spot_price:.2f}, "
                    f"contract_value={self.contract_value}) — defaulting lot size to 1"
                )

        if self.sl_pct is not None:
            self.sl_threshold = self.entry_premium * self.sl_pct
            sl_threshold_str = f"${self.sl_threshold:.4f}"
        else:
            self.sl_threshold = float('inf')
            sl_threshold_str = "None (Disabled)"

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
            self.is_position_open = True  # Simulate for monitoring

            # Send entry alert even in disabled mode
            margin_usd = 2 * self._calculate_margin_per_leg(self.spot_price) * self.lot_size
            self.notifier.send_entry_alert(
                underlying=self.underlying,
                strategy_name="Short Straddle",
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

        # Record start time for transaction queries
        self.entry_time_us = int(time.time() * 1_000_000)

        # Set leverage on both option products if configured
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

        # Place SELL orders (short straddle)
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
            self.notifier.send_error(alert_title, err_str)
            return

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
            err_str = str(e)
            alert_title = (
                "Market Disrupted — Put Order Failed"
                if "market_disrupted_cancel_only_mode" in err_str.lower()
                else "Put Order Failed"
            )
            logger.error(f"Failed to place Put order: {e}")
            self.notifier.send_error(alert_title, err_str)
            # Close the call leg if put fails
            try:
                self.client.close_position(self.call_product_id)
                logger.info("Rolled back Call position after Put failure")
            except Exception as rollback_err:
                logger.error(f"Failed to rollback Call position: {rollback_err}")
            return

        self.is_position_open = True

        # ---------------------------------------------------------------
        # Fetch actual execution fill prices from exchange
        # Priority: get_fills() → get_order().avg_fill_price → mark snapshot
        # ---------------------------------------------------------------
        if self.mode != "paper":
            fill_wait_sec = 3
            logger.info(f"Waiting {fill_wait_sec}s for fills to propagate...")
            time.sleep(fill_wait_sec)

            now_us = int(time.time() * 1_000_000)

            # --- Call leg ---
            call_fill_price: Optional[float] = None
            if call_order and call_order.get("id"):
                call_order_id = int(call_order.get("id"))

                # Primary: /v2/fills
                call_fills = self.client.get_fills(
                    product_id=self.call_product_id,
                    order_id=call_order_id,
                    start_time_us=self.entry_time_us - 10 * 1_000_000,
                    end_time_us=now_us,
                )
                call_fill_price = self.client.get_weighted_avg_fill_price(call_fills)
                if call_fill_price is not None:
                    logger.info(
                        f"[ENTRY FILL] Call: ${call_fill_price:.4f} "
                        f"(exchange fills, {len(call_fills)} fill(s)) | "
                        f"Mark was: ${self.call_entry_mark:.4f}"
                    )
                else:
                    # Secondary: get_order avg_fill_price
                    try:
                        call_order_details = self.client.get_order(call_order_id)
                        # NOTE: Delta Exchange API uses 'average_fill_price' (not 'avg_fill_price')
                        avg_fill = call_order_details.get("average_fill_price")
                        if avg_fill:
                            call_fill_price = float(avg_fill)
                            logger.info(
                                f"[ENTRY FILL] Call: ${call_fill_price:.4f} "
                                f"(get_order fallback) | Mark was: ${self.call_entry_mark:.4f}"
                            )
                        else:
                            logger.warning(
                                f"[ENTRY FILL] Call: average_fill_price missing from order — "
                                f"keeping mark snapshot ${self.call_entry_mark:.4f}. "
                                f"Raw order keys: {list(call_order_details.keys())}"
                            )
                    except Exception as e:
                        logger.warning(f"[ENTRY FILL] Call: get_order failed: {e} — keeping mark snapshot")

            if call_fill_price is not None:
                self.call_entry_premium = call_fill_price

            # --- Put leg ---
            put_fill_price: Optional[float] = None
            if put_order and put_order.get("id"):
                put_order_id = int(put_order.get("id"))

                # Primary: /v2/fills
                put_fills = self.client.get_fills(
                    product_id=self.put_product_id,
                    order_id=put_order_id,
                    start_time_us=self.entry_time_us - 10 * 1_000_000,
                    end_time_us=now_us,
                )
                put_fill_price = self.client.get_weighted_avg_fill_price(put_fills)
                if put_fill_price is not None:
                    logger.info(
                        f"[ENTRY FILL] Put: ${put_fill_price:.4f} "
                        f"(exchange fills, {len(put_fills)} fill(s)) | "
                        f"Mark was: ${self.put_entry_mark:.4f}"
                    )
                else:
                    # Secondary: get_order avg_fill_price
                    try:
                        put_order_details = self.client.get_order(put_order_id)
                        # NOTE: Delta Exchange API uses 'average_fill_price' (not 'avg_fill_price')
                        avg_fill = put_order_details.get("average_fill_price")
                        if avg_fill:
                            put_fill_price = float(avg_fill)
                            logger.info(
                                f"[ENTRY FILL] Put: ${put_fill_price:.4f} "
                                f"(get_order fallback) | Mark was: ${self.put_entry_mark:.4f}"
                            )
                        else:
                            logger.warning(
                                f"[ENTRY FILL] Put: average_fill_price missing from order — "
                                f"keeping mark snapshot ${self.put_entry_mark:.4f}. "
                                f"Raw order keys: {list(put_order_details.keys())}"
                            )
                    except Exception as e:
                        logger.warning(f"[ENTRY FILL] Put: get_order failed: {e} — keeping mark snapshot")

            if put_fill_price is not None:
                self.put_entry_premium = put_fill_price

        # Calculate entry slippage: (Mark - Fill) since we sell options
        call_entry_slippage = self.call_entry_mark - self.call_entry_premium
        put_entry_slippage = self.put_entry_mark - self.put_entry_premium
        self.entry_slippage_usd = (call_entry_slippage + put_entry_slippage) * self.lot_size * self.contract_value
        logger.info(f"Calculated Entry Slippage: Call=${call_entry_slippage:.4f}, Put=${put_entry_slippage:.4f}, Total=${self.entry_slippage_usd:.4f} USD")

        self.entry_premium = self.call_entry_premium + self.put_entry_premium
        self.sl_threshold = self.entry_premium * self.sl_pct if self.sl_pct is not None else None

        # Send Discord entry notification
        margin_usd = 2 * self._calculate_margin_per_leg(self.spot_price) * self.lot_size
        self.notifier.send_entry_alert(
            underlying=self.underlying,
            strategy_name="Short Straddle",
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
            strategy_name="short_straddle",
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

        logger.info("✅ Straddle entry complete")

    def _monitor_stop_loss(self) -> bool:
        """Monitor the combined position for stop-loss.

        Polls every monitor_interval seconds until:
        - SL is hit (returns True)
        - Exit time is reached (returns False)

        Returns:
            True if SL was hit and positions were closed, False otherwise
        """
        logger.info("=" * 60)
        logger.info("STEP 2: MONITORING — Watching for combined stop-loss")
        logger.info("=" * 60)

        if not self.is_position_open:
            return False

        # Parse exit time
        exit_time_str = self.config.strategy.exit_time
        now = datetime.now(IST)
        exit_h, exit_m = map(int, exit_time_str.split(":"))
        exit_time = now.replace(hour=exit_h, minute=exit_m, second=0, microsecond=0)

        sl_threshold_str = f"${self.sl_threshold:.4f}" if self.sl_pct is not None else "None (Disabled)"
        logger.info(f"Monitoring until {exit_time_str} IST (SL threshold: {sl_threshold_str})")

        while True:
            now = datetime.now(IST)

            # Check if exit time reached
            if now >= exit_time:
                logger.info("Exit time reached — SL was not hit")
                return False

            # Calculate current MTM
            try:
                current_call_premium = self._get_current_premium(self.call_product_id, self.call_symbol)
                current_put_premium = self._get_current_premium(self.put_product_id, self.put_symbol)
                current_total = current_call_premium + current_put_premium

                # For a short straddle, loss = current premium - entry premium
                # (we sold at entry_premium, to close we'd buy at current_total)
                mtm_loss = current_total - self.entry_premium

                # Track max loss
                if mtm_loss > self.max_mtm_loss:
                    self.max_mtm_loss = mtm_loss

                loss_pct = (mtm_loss / self.entry_premium) * 100 if self.entry_premium > 0 else 0

                logger.debug(
                    f"MTM — Call: ${current_call_premium:.4f}, Put: ${current_put_premium:.4f}, "
                    f"Total: ${current_total:.4f}, Loss: ${mtm_loss:.4f} ({loss_pct:.1f}% of premium)"
                )

                # Check SL: if loss exceeds threshold (skip if SL is disabled)
                if self.sl_threshold is not None and mtm_loss >= self.sl_threshold:
                    logger.warning(
                        f"⚠️ STOP-LOSS HIT! Loss: ${mtm_loss:.4f} >= Threshold: ${self.sl_threshold:.4f} "
                        f"({loss_pct:.1f}% of premium)"
                    )

                    # Send SL alert
                    self.notifier.send_sl_alert(
                        underlying=self.underlying,
                        current_loss=mtm_loss,
                        sl_threshold=self.sl_threshold,
                        entry_premium=self.entry_premium,
                        loss_pct=loss_pct,
                        mode=self.mode,
                    )

                    # Close positions
                    self._execute_exit(reason="stop_loss_hit")
                    return True

            except Exception as e:
                logger.warning(f"Error during MTM monitoring: {e}")

            # Sleep before next poll
            time.sleep(self.monitor_interval)

    def _get_current_premium(self, product_id: int, symbol: str) -> float:
        """Get the current mark price (premium) for an option.

        Args:
            product_id: Option product ID
            symbol: Option symbol

        Returns:
            Current mark price
        """
        try:
            ticker = self.client.get_ticker(symbol)
            mark_price = float(ticker.get("mark_price", 0))
            if mark_price > 0:
                return mark_price
        except Exception:
            pass

        # Fallback
        return self.client.get_option_mark_price(product_id)

    def _execute_exit(self, reason: str = "scheduled_exit") -> None:
        """Close both legs of the straddle.

        Args:
            reason: Exit reason ('scheduled_exit', 'stop_loss_hit', 'emergency')
        """
        logger.info("=" * 60)
        logger.info(f"STEP 3: EXIT — Closing straddle (reason: {reason})")
        logger.info("=" * 60)

        if not self.is_position_open:
            logger.info("No open positions to close")
            return

        # Determine if we should hold to expiry (auto-settle) or place manual orders
        let_settle = (reason == "scheduled_exit" and self.config.strategy.exit_time == "17:30")

        # Get exit premiums before closing (will be overridden with actual fills or settlement intrinsic value)
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
            # Close call position
            try:
                logger.info(f"Closing Call position: {self.call_symbol}")
                call_response = self.client.close_position(self.call_product_id)
                call_exit_order_id = str(call_response.get("id"))
                logger.info(f"Call position closed: {call_exit_order_id}")
            except Exception as e:
                logger.error(f"Failed to close Call position: {e}")
                self.notifier.send_error("Call Exit Failed", str(e))

            # Close put position
            try:
                logger.info(f"Closing Put position: {self.put_symbol}")
                put_response = self.client.close_position(self.put_product_id)
                put_exit_order_id = str(put_response.get("id"))
                logger.info(f"Put position closed: {put_exit_order_id}")
            except Exception as e:
                logger.error(f"Failed to close Put position: {e}")
                self.notifier.send_error("Put Exit Failed", str(e))

            # ---------------------------------------------------------------
            # Fetch actual exit fill prices from exchange
            # Priority: get_fills() → get_order().avg_fill_price → mark snapshot
            # ---------------------------------------------------------------
            if self.mode != "paper":
                fill_wait_sec = 3
                logger.info(f"Waiting {fill_wait_sec}s for exit fills to propagate...")
                time.sleep(fill_wait_sec)

                now_us_exit = int(time.time() * 1_000_000)

                # --- Call exit leg ---
                call_exit_fill: Optional[float] = None
                if call_exit_order_id:
                    call_exit_fills = self.client.get_fills(
                        product_id=self.call_product_id,
                        order_id=int(call_exit_order_id),
                        start_time_us=self.entry_time_us,
                        end_time_us=now_us_exit,
                    )
                    call_exit_fill = self.client.get_weighted_avg_fill_price(call_exit_fills)
                    if call_exit_fill is not None:
                        logger.info(
                            f"[EXIT FILL] Call: ${call_exit_fill:.4f} "
                            f"(exchange fills, {len(call_exit_fills)} fill(s)) | "
                            f"Mark was: ${exit_call_mark:.4f}"
                        )
                    else:
                        try:
                            call_exit_details = self.client.get_order(int(call_exit_order_id))
                            # NOTE: Delta Exchange API uses 'average_fill_price' (not 'avg_fill_price')
                            avg_fill = call_exit_details.get("average_fill_price")
                            if avg_fill:
                                call_exit_fill = float(avg_fill)
                                logger.info(
                                    f"[EXIT FILL] Call: ${call_exit_fill:.4f} "
                                    f"(get_order fallback) | Mark was: ${exit_call_mark:.4f}"
                                )
                            else:
                                logger.warning(
                                    f"[EXIT FILL] Call: average_fill_price missing — "
                                    f"keeping mark snapshot ${exit_call_mark:.4f}. "
                                    f"Raw order keys: {list(call_exit_details.keys())}"
                                )
                        except Exception as e:
                            logger.warning(f"[EXIT FILL] Call: get_order failed: {e} — keeping mark snapshot")

                if call_exit_fill is not None:
                    exit_call_premium = call_exit_fill

                # --- Put exit leg ---
                put_exit_fill: Optional[float] = None
                if put_exit_order_id:
                    put_exit_fills = self.client.get_fills(
                        product_id=self.put_product_id,
                        order_id=int(put_exit_order_id),
                        start_time_us=self.entry_time_us,
                        end_time_us=now_us_exit,
                    )
                    put_exit_fill = self.client.get_weighted_avg_fill_price(put_exit_fills)
                    if put_exit_fill is not None:
                        logger.info(
                            f"[EXIT FILL] Put: ${put_exit_fill:.4f} "
                            f"(exchange fills, {len(put_exit_fills)} fill(s)) | "
                            f"Mark was: ${exit_put_mark:.4f}"
                        )
                    else:
                        try:
                            put_exit_details = self.client.get_order(int(put_exit_order_id))
                            # NOTE: Delta Exchange API uses 'average_fill_price' (not 'avg_fill_price')
                            avg_fill = put_exit_details.get("average_fill_price")
                            if avg_fill:
                                put_exit_fill = float(avg_fill)
                                logger.info(
                                    f"[EXIT FILL] Put: ${put_exit_fill:.4f} "
                                    f"(get_order fallback) | Mark was: ${exit_put_mark:.4f}"
                                )
                            else:
                                logger.warning(
                                    f"[EXIT FILL] Put: average_fill_price missing — "
                                    f"keeping mark snapshot ${exit_put_mark:.4f}. "
                                    f"Raw order keys: {list(put_exit_details.keys())}"
                                )
                        except Exception as e:
                            logger.warning(f"[EXIT FILL] Put: get_order failed: {e} — keeping mark snapshot")

                if put_exit_fill is not None:
                    exit_put_premium = put_exit_fill

        elif let_settle:
            logger.info("Holding straddle to expiry (auto-settlement). No closing orders will be placed.")
            # ---------------------------------------------------------------
            # Fetch actual settlement values from the exchange ledger.
            # Delta Exchange credits/debits the settlement as a 'pnl'
            # wallet transaction — this reflects the real index settlement
            # price, NOT the live API spot at expiry time.
            # ---------------------------------------------------------------
            settle_wait_sec = 20
            logger.info(f"Waiting {settle_wait_sec}s for settlement transactions to post to ledger...")
            time.sleep(settle_wait_sec)

            now_us_settle = int(time.time() * 1_000_000)
            start_us_settle = self.entry_time_us - (60 * 1_000_000)

            call_settle_usd = self.client.get_settlement_pnl_transactions(
                start_time_us=start_us_settle,
                end_time_us=now_us_settle,
                product_id=self.call_product_id,
            )
            put_settle_usd = self.client.get_settlement_pnl_transactions(
                start_time_us=start_us_settle,
                end_time_us=now_us_settle,
                product_id=self.put_product_id,
            )

            # get_settlement_pnl_transactions() now returns settlement price in POINTS
            # directly from /v2/fills (fill_type='settlement'). No USD conversion needed.
            if call_settle_usd is not None:
                exit_call_premium = float(call_settle_usd)  # already in points
                logger.info(
                    f"[EXIT SETTLE] Call: settlement_price={exit_call_premium:.4f} pts (exchange fill)"
                )
            else:
                # Fallback: intrinsic from live spot (with clear warning)
                try:
                    expiry_spot = self.client.get_spot_price(self.underlying)
                    exit_call_premium = max(0.0, expiry_spot - self.atm_strike)
                    logger.warning(
                        f"[EXIT SETTLE] Call: ledger not available — using spot-based intrinsic "
                        f"${exit_call_premium:.4f} (spot={expiry_spot:,.2f}). MAY DIFFER FROM EXCHANGE."
                    )
                except Exception:
                    exit_call_premium = self._get_current_premium(self.call_product_id, self.call_symbol)
                    logger.warning(f"[EXIT SETTLE] Call: using mark price fallback ${exit_call_premium:.4f}")

            if put_settle_usd is not None:
                exit_put_premium = float(put_settle_usd)  # already in points
                logger.info(
                    f"[EXIT SETTLE] Put: settlement_price={exit_put_premium:.4f} pts (exchange fill)"
                )
            else:
                try:
                    expiry_spot = self.client.get_spot_price(self.underlying)
                    exit_put_premium = max(0.0, self.atm_strike - expiry_spot)
                    logger.warning(
                        f"[EXIT SETTLE] Put: ledger not available — using spot-based intrinsic "
                        f"${exit_put_premium:.4f} (spot={expiry_spot:,.2f}). MAY DIFFER FROM EXCHANGE."
                    )
                except Exception:
                    exit_put_premium = self._get_current_premium(self.put_product_id, self.put_symbol)
                    logger.warning(f"[EXIT SETTLE] Put: using mark price fallback ${exit_put_premium:.4f}")

        else:
            logger.warning("[DISABLED] Order placement disabled — simulating exit")

        # Calculate exit slippage: (Fill - Mark) since we buy back options.
        # Auto-settled positions have 0.0 slippage on exit.
        if let_settle:
            call_exit_slippage = 0.0
            put_exit_slippage = 0.0
        else:
            call_exit_slippage = exit_call_premium - exit_call_mark
            put_exit_slippage = exit_put_premium - exit_put_mark

        exit_slippage_usd = (call_exit_slippage + put_exit_slippage) * self.lot_size * self.contract_value
        total_slippage_usd = self.entry_slippage_usd + exit_slippage_usd

        logger.info(f"Calculated Exit Slippage: Call=${call_exit_slippage:.4f}, Put=${put_exit_slippage:.4f}, Total=${exit_slippage_usd:.4f} USD")
        logger.info(f"Total Trade Slippage: ${total_slippage_usd:.4f} USD")

        exit_total = exit_call_premium + exit_put_premium
        pnl_points = self.entry_premium - exit_total
        realized_pnl_usd = pnl_points * self.lot_size * self.contract_value

        # Cross-check realized PnL against exchange ledger 'pnl' transactions
        # For auto-settle this is already embedded in settlement_pnl_transactions above.
        # For early-close, query the 'realized_pnl' transaction type.
        exchange_realized_pnl: Optional[float] = None
        if self.order_placement_enabled and self.mode != "paper" and not let_settle:
            try:
                now_us_pnl = int(time.time() * 1_000_000)
                start_us_pnl = self.entry_time_us - (60 * 1_000_000)
                for pnl_type in ("realized_pnl", "pnl"):
                    call_pnl_txns = self.client.get_wallet_transactions(
                        transaction_types=pnl_type,
                        start_time_us=start_us_pnl,
                        end_time_us=now_us_pnl,
                        product_id=self.call_product_id,
                    )
                    put_pnl_txns = self.client.get_wallet_transactions(
                        transaction_types=pnl_type,
                        start_time_us=start_us_pnl,
                        end_time_us=now_us_pnl,
                        product_id=self.put_product_id,
                    )
                    if call_pnl_txns or put_pnl_txns:
                        exchange_realized_pnl = (
                            sum(float(t.get("amount", 0)) for t in call_pnl_txns)
                            + sum(float(t.get("amount", 0)) for t in put_pnl_txns)
                        )
                        logger.info(
                            f"[PNL CROSS-CHECK] Exchange ledger PnL (type={pnl_type}): "
                            f"${exchange_realized_pnl:.4f} | Calculated: ${realized_pnl_usd:.4f} | "
                            f"Diff: ${exchange_realized_pnl - realized_pnl_usd:.4f}"
                        )
                        break
                if exchange_realized_pnl is None:
                    logger.info("[PNL CROSS-CHECK] No realized_pnl/pnl transactions found — using calculated value")
            except Exception as e:
                logger.warning(f"[PNL CROSS-CHECK] Failed to fetch exchange PnL: {e}")

        self.is_position_open = False

        # Query trading fees (commissions) from the exchange ledger
        trading_fees = 0.0
        if self.order_placement_enabled and self.mode != "paper":
            try:
                sleep_sec = 10 if let_settle else 3
                logger.info(f"Waiting {sleep_sec} seconds for exchange ledger to update commission logs...")
                time.sleep(sleep_sec)
                now_us = int(time.time() * 1_000_000)
                # Query from entry minus 60s up to now
                start_us = self.entry_time_us - (60 * 1_000_000)

                call_txns = self.client.get_trading_fee_transactions(
                    start_time_us=start_us,
                    end_time_us=now_us,
                    product_id=self.call_product_id
                )
                put_txns = self.client.get_trading_fee_transactions(
                    start_time_us=start_us,
                    end_time_us=now_us,
                    product_id=self.put_product_id
                )

                call_comm = sum(abs(float(t.get("amount", 0))) for t in call_txns)
                put_comm = sum(abs(float(t.get("amount", 0))) for t in put_txns)
                trading_fees = call_comm + put_comm

                logger.info(f"Retrieved actual commissions from ledger: Call=${call_comm:.4f}, Put=${put_comm:.4f}, Total=${trading_fees:.4f}")
            except Exception as e:
                logger.error(f"Failed to fetch actual commissions from ledger: {e}")
                trading_fees = None

        # Fallback to simulated fees if live ledger query was skipped/failed
        if trading_fees is None or self.mode == "paper":
            if let_settle:
                # Entry fees: taker rate of 0.03% on entry underlying notional value
                entry_fee = 2 * self.lot_size * self.contract_value * 0.0003 * self.spot_price
                # Settlement fees for ITM leg: 0.01% of spot, capped at 10% of payout
                settle_fee_rate = 0.0001
                raw_settle_fee = self.lot_size * self.contract_value * settle_fee_rate * self.spot_price
                settle_cap = 0.10 * exit_total * self.lot_size * self.contract_value
                settle_fee = min(raw_settle_fee, settle_cap)
                trading_fees = entry_fee + settle_fee
                logger.info(
                    f"Simulated Hold to Expiry fees: Entry=${entry_fee:.4f}, "
                    f"Settle=${settle_fee:.4f}, Total=${trading_fees:.4f}"
                )
            else:
                # Closed early (e.g. SL hit): taker fee is typically 0.03% of notional on entry and exit
                entry_notional = self.entry_premium * self.lot_size * self.contract_value
                exit_notional = exit_total * self.lot_size * self.contract_value
                trading_fees = (entry_notional + exit_notional) * 0.0003
                logger.info(f"Simulated trading fees (0.03% taker rate): ${trading_fees:.4f}")

        logger.info(
            f"Exit complete — "
            f"Entry: ${self.entry_premium:.4f}, Exit: ${exit_total:.4f}, "
            f"PnL Points: {pnl_points:+.4f}, PnL USD: ${realized_pnl_usd:+.4f}, Fees: ${trading_fees:.4f}"
        )

        # Send exit notification (pass USD P&L, fees, and exchange cross-check PnL)
        self.notifier.send_exit_alert(
            underlying=self.underlying,
            exit_reason=reason,
            entry_premium=self.entry_premium,
            exit_premium=exit_total,
            realized_pnl=realized_pnl_usd,
            call_symbol=self.call_symbol,
            put_symbol=self.put_symbol,
            exit_call_premium=exit_call_premium,
            exit_put_premium=exit_put_premium,
            mode=self.mode,
            exit_slippage_usd=exit_slippage_usd,
            total_slippage_usd=total_slippage_usd,
            exchange_realized_pnl=exchange_realized_pnl,
            is_exchange_sourced=(let_settle or exchange_realized_pnl is not None),
        )

        # Journal to Firestore (pnl in USD, additional metrics in kwargs)
        journal_straddle_exit(
            trade_id=self.trade_id,
            exit_reason=reason,
            exit_call_premium=exit_call_premium,
            exit_put_premium=exit_put_premium,
            realized_pnl=realized_pnl_usd,
            max_mtm_loss=self.max_mtm_loss * self.lot_size * self.contract_value,
            call_exit_order_id=call_exit_order_id,
            put_exit_order_id=put_exit_order_id,
            pnl_points=pnl_points,
            trading_fees=trading_fees,
            contract_value=self.contract_value,
            exit_slippage_usd=exit_slippage_usd,
            total_slippage_usd=total_slippage_usd,
        )

        logger.info("✅ Straddle exit complete")

    def _emergency_exit(self) -> None:
        """Attempt to close all positions in case of an error."""
        logger.warning("EMERGENCY EXIT — Attempting to close all positions")

        if not self.order_placement_enabled:
            logger.warning("[DISABLED] Emergency exit — no positions to close (orders disabled)")
            self.is_position_open = False
            return

        if self.call_product_id:
            try:
                self.client.close_position(self.call_product_id)
                logger.info("Emergency: Call position closed")
            except Exception as e:
                logger.error(f"Emergency: Failed to close Call: {e}")

        if self.put_product_id:
            try:
                self.client.close_position(self.put_product_id)
                logger.info("Emergency: Put position closed")
            except Exception as e:
                logger.error(f"Emergency: Failed to close Put: {e}")

        self.is_position_open = False
