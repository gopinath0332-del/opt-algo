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
        self.lot_size = config.strategy.lot_size
        self.leverage = config.strategy.leverage
        self.sl_pct = config.strategy.stop_loss.value / 100.0  # Convert 50 -> 0.5
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

    def run(self) -> None:
        """Execute the full strategy cycle: entry → monitor → exit."""
        now = datetime.now(IST)
        logger.info(
            f"Short Straddle strategy starting",
            underlying=self.underlying,
            mode=self.mode,
            time=now.strftime("%Y-%m-%d %H:%M:%S IST"),
            order_placement=self.order_placement_enabled,
        )

        try:
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

        logger.info(
            f"ATM Strike: {self.atm_strike} | "
            f"Call: {self.call_symbol} (ID: {self.call_product_id}) | "
            f"Put: {self.put_symbol} (ID: {self.put_product_id})"
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
        self.sl_threshold = self.entry_premium * self.sl_pct

        logger.info(
            f"Entry premiums — Call: ${self.call_entry_premium:.4f}, "
            f"Put: ${self.put_entry_premium:.4f}, "
            f"Total: ${self.entry_premium:.4f}, "
            f"SL Threshold (50%): ${self.sl_threshold:.4f}"
        )

        if not self.order_placement_enabled:
            logger.warning(
                "[DISABLED] Order placement is disabled (ENABLE_ORDER_PLACEMENT=false). "
                "Logging signal only — no orders placed."
            )
            self.is_position_open = True  # Simulate for monitoring

            # Send entry alert even in disabled mode
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
                leverage=self.leverage,
                sl_threshold=self.sl_threshold,
                mode=self.mode,
            )
            return

        # Set leverage on both option products
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
            logger.error(f"Failed to place Call order: {e}")
            self.notifier.send_error("Call Order Failed", str(e))
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
            logger.error(f"Failed to place Put order: {e}")
            self.notifier.send_error("Put Order Failed", str(e))
            # Close the call leg if put fails
            try:
                self.client.close_position(self.call_product_id)
                logger.info("Rolled back Call position after Put failure")
            except Exception as rollback_err:
                logger.error(f"Failed to rollback Call position: {rollback_err}")
            return

        self.is_position_open = True

        # Re-fetch premiums after fills (execution prices)
        try:
            time.sleep(1)  # Brief delay for settlement
            call_ticker = self.client.get_ticker(self.call_symbol)
            put_ticker = self.client.get_ticker(self.put_symbol)
            self.call_entry_premium = float(call_ticker.get("mark_price", self.call_entry_premium))
            self.put_entry_premium = float(put_ticker.get("mark_price", self.put_entry_premium))
            self.entry_premium = self.call_entry_premium + self.put_entry_premium
            self.sl_threshold = self.entry_premium * self.sl_pct
        except Exception:
            pass

        # Send Discord entry notification
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
            leverage=self.leverage,
            sl_threshold=self.sl_threshold,
            mode=self.mode,
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

        logger.info(f"Monitoring until {exit_time_str} IST (SL threshold: ${self.sl_threshold:.4f})")

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

                # Check SL: if loss exceeds threshold
                if mtm_loss >= self.sl_threshold:
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

        # Get exit premiums before closing
        exit_call_premium = self._get_current_premium(self.call_product_id, self.call_symbol)
        exit_put_premium = self._get_current_premium(self.put_product_id, self.put_symbol)
        exit_total = exit_call_premium + exit_put_premium

        # Calculate P&L: for short, profit = entry - exit
        realized_pnl = self.entry_premium - exit_total

        call_exit_order_id = None
        put_exit_order_id = None

        if self.order_placement_enabled:
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
        else:
            logger.warning("[DISABLED] Order placement disabled — simulating exit")

        self.is_position_open = False

        logger.info(
            f"Exit complete — "
            f"Entry: ${self.entry_premium:.4f}, Exit: ${exit_total:.4f}, "
            f"P&L: ${realized_pnl:+.4f}"
        )

        # Send exit notification
        self.notifier.send_exit_alert(
            underlying=self.underlying,
            exit_reason=reason,
            entry_premium=self.entry_premium,
            exit_premium=exit_total,
            realized_pnl=realized_pnl,
            call_symbol=self.call_symbol,
            put_symbol=self.put_symbol,
            exit_call_premium=exit_call_premium,
            exit_put_premium=exit_put_premium,
            mode=self.mode,
        )

        # Journal to Firestore
        journal_straddle_exit(
            trade_id=self.trade_id,
            exit_reason=reason,
            exit_call_premium=exit_call_premium,
            exit_put_premium=exit_put_premium,
            realized_pnl=realized_pnl,
            max_mtm_loss=self.max_mtm_loss,
            call_exit_order_id=call_exit_order_id,
            put_exit_order_id=put_exit_order_id,
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
