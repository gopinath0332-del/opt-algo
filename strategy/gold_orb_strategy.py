"""Gold ORB (Opening Range Breakout) Strategy for XAUTUSD futures on Delta Exchange.

Strategy Logic:
- Symbol:     XAUTUSD
- Timeframe:  1H
- ORB Window: 06:30–07:30 IST (06:00 IST candle in normalized UTC data)
- H1 / L1:    High / Low of the ORB candle
- Long Entry: Close of any subsequent candle > H1  → LONG at close price
- Short Entry:Close of any subsequent candle < L1  → SHORT at close price
- SL/TP 1:1:
    LONG  → sl = L1,  tp = entry + (entry − L1)
    SHORT → sl = H1,  tp = entry − (H1 − entry)
- Lot Size:   Fixed 1000 contracts
- Leverage:   100x
- Max 1 trade per day; resets at the next ORB candle
"""

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.config import get_config
from core.firestore_client import journal_orb_entry, journal_orb_exit
from core.logger import get_logger
from notifications.discord import DiscordNotifier

logger = get_logger(__name__)

# IST = UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

# ORB candle opens at 06:30 IST every day.
# In epoch-normalized hourly data, the 06:30 IST candle decodes to hour=6, minute=0.
ORB_HOUR_IST = 6
ORB_MINUTE_IST = 0

STRATEGY_NAME = "Gold ORB"
FIXED_LOT_SIZE = 1000
LEVERAGE = 100


class GoldOrbStrategy:
    """Gold XAUTUSD Opening Range Breakout — 1H, fixed 1000 lots, 100x leverage."""

    def __init__(self, symbol: str = "XAUTUSD"):
        self.symbol = symbol
        self.strategy_name = "gold_orb"

        config = get_config()
        cfg = getattr(config, "settings", {}).get("strategies", {}).get("gold_orb", {}) if hasattr(config, "settings") else {}

        self.trade_mode: str = cfg.get("trade_mode", "Both")
        self.allow_long = self.trade_mode in ("Long", "Both")
        self.allow_short = self.trade_mode in ("Short", "Both")

        self.fixed_lot_size: int = int(cfg.get("fixed_lot_size", FIXED_LOT_SIZE))
        self.leverage: int = int(cfg.get("leverage", LEVERAGE))
        self.timeframe = "1h"
        self.indicator_label = "ORB"

        # ---- ORB State (reset each day) ----
        self.orb_h1: Optional[float] = None
        self.orb_l1: Optional[float] = None
        self.orb_close: Optional[float] = None
        self.orb_date: Optional[date] = None
        self.trade_taken_today: bool = False
        self.pct_threshold: float = float(cfg.get("pct_threshold", 0.005))  # 0.5% movement threshold
        self.rr_ratio: float = float(cfg.get("rr_ratio", 1.25))              # 1:1.25 Risk/Reward ratio


        # ---- Active Trade State ----
        self.current_position: int = 0  # 1 for Long, -1 for Short, 0 for Flat
        self.entry_price: Optional[float] = None
        self.tp_price: Optional[float] = None
        self.sl_price: Optional[float] = None
        self.entry_side: Optional[str] = None
        self.trade_id: Optional[str] = None
        self.trades: List[Dict[str, Any]] = []

    def calculate_indicators(self, df: pd.DataFrame, current_time=None):
        """Returns ORB levels."""
        return self.orb_h1, self.orb_l1

    def check_signals(
        self,
        df: pd.DataFrame,
        current_time_ms: float = None,
        live_pos_data: Optional[Dict] = None,
    ) -> Tuple[Optional[str], str]:
        """
        Evaluate the last closed candle for a 0.5% movement breakout signal from ORB close.
        """
        if df.empty:
            return None, ""

        last = df.iloc[-1]
        candle_ts = float(last["time"]) if "time" in last else float(last.name.timestamp())
        candle_dt_ist = datetime.fromtimestamp(candle_ts, tz=IST)
        candle_date = candle_dt_ist.date()

        # ---- Detect ORB candle ----
        if (
            candle_dt_ist.hour == ORB_HOUR_IST
            and candle_dt_ist.minute == ORB_MINUTE_IST
        ):
            self._set_orb(last, candle_date)
            return None, ""

        if self.orb_date and candle_date > self.orb_date:
            self._reset_daily_state()

        if self.orb_close is None:
            return None, "No ORB candle seen yet"
        if self.trade_taken_today or self.current_position != 0:
            return None, "Trade already taken today"

        close = float(last["close"])
        long_trigger = self.orb_close * (1 + self.pct_threshold)
        short_trigger = self.orb_close * (1 - self.pct_threshold)

        if self.allow_long and close >= long_trigger:
            reason = f"0.5% upward move from ORB close ({self.orb_close:.2f} -> {close:.2f})"
            return "ENTRY_LONG", reason

        if self.allow_short and close <= short_trigger:
            reason = f"0.5% downward move from ORB close ({self.orb_close:.2f} -> {close:.2f})"
            return "ENTRY_SHORT", reason

        return None, ""


    def update_position_state(
        self,
        action: str,
        price: float = 0.0,
        reason: str = "",
    ):
        """Called after order execution to record state, journal, and alert."""
        dt_ist = datetime.now(tz=IST)
        time_str = dt_ist.strftime("%d-%m-%y %H:%M IST")

        if action == "ENTRY_LONG":
            self._open_trade("long", price, reason, time_str)
        elif action == "ENTRY_SHORT":
            self._open_trade("short", price, reason, time_str)
        elif action in ("EXIT_LONG", "EXIT_SHORT"):
            self._close_trade(price, reason, time_str)

    def close_eod_position(self, client, product_id: int, current_price: float):
        """
        Close any remaining open position at End-of-Day (05:30 IST / before next ORB).
        
        1. Cancels active bracket orders (TP/SL) on Delta Exchange.
        2. Places market order to close position.
        3. Journals exit to Firestore & notifies Discord.
        """
        logger.info(f"[{self.symbol}] Executing EOD position closure for product {product_id}")
        
        # 1. Cancel open bracket orders
        try:
            client.cancel_all_orders(product_id)
            logger.info(f"[{self.symbol}] Cancelled active bracket orders for product {product_id}")
        except Exception as e:
            logger.warning(f"[{self.symbol}] Error cancelling bracket orders: {e}")

        # 2. Close position via market order
        try:
            res = client.close_position(product_id)
            logger.info(f"[{self.symbol}] Closed EOD position: {res}")
        except Exception as e:
            logger.error(f"[{self.symbol}] Error closing position: {e}")

        # 3. Update state & notifications
        action = "EXIT_LONG" if self.current_position == 1 else "EXIT_SHORT"
        self.update_position_state(action, price=current_price, reason="EOD Session Close")


    def run_backtest(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """Run a full candle-by-candle backtest using 0.5% move from ORB close."""
        self.trades = []

        def _fmt(ts_sec: float) -> str:
            return datetime.fromtimestamp(ts_sec).strftime("%d-%m-%y %H:%M")

        df = df.copy()
        df["_dt_ist"] = df["time"].apply(
            lambda t: datetime.fromtimestamp(float(t), tz=IST)
        )
        df["_date_ist"] = df["_dt_ist"].apply(lambda d: d.date())

        for _day_date, day_df in df.groupby("_date_ist", sort=True):
            day_df = day_df.reset_index(drop=True)
            orb_row = None

            for i, row in day_df.iterrows():
                dt_ist = row["_dt_ist"]

                if dt_ist.hour == ORB_HOUR_IST and dt_ist.minute == ORB_MINUTE_IST:
                    orb_row = row
                    continue

                if orb_row is None:
                    continue

                h1 = float(orb_row["high"])
                l1 = float(orb_row["low"])
                orb_close = float(orb_row["close"])
                close = float(row["close"])

                long_trigger = orb_close * (1 + self.pct_threshold)
                short_trigger = orb_close * (1 - self.pct_threshold)

                signal = None
                if self.allow_long and close >= long_trigger:
                    signal = "LONG"
                elif self.allow_short and close <= short_trigger:
                    signal = "SHORT"

                if signal is None:
                    continue

                entry = close
                sl = l1 if signal == "LONG" else h1
                risk = (entry - sl) if signal == "LONG" else (sl - entry)
                if risk <= 0:
                    risk = entry * self.pct_threshold
                    sl = entry - risk if signal == "LONG" else entry + risk

                tp = entry + (risk * self.rr_ratio) if signal == "LONG" else entry - (risk * self.rr_ratio)


                entry_time_str = _fmt(float(row["time"]))

                result = "OPEN"
                exit_price = float(day_df.iloc[-1]["close"])
                exit_time_str = _fmt(float(day_df.iloc[-1]["time"]))

                for j in range(i + 1, len(day_df)):
                    sim = day_df.iloc[j]
                    c_low = float(sim["low"])
                    c_high = float(sim["high"])

                    if signal == "LONG":
                        sl_hit = c_low <= sl
                        tp_hit = c_high >= tp
                    else:
                        sl_hit = c_high >= sl
                        tp_hit = c_low <= tp

                    if sl_hit and tp_hit:
                        result = "SL HIT"
                        exit_price = sl
                        exit_time_str = _fmt(float(sim["time"]))
                        break
                    elif sl_hit:
                        result = "SL HIT"
                        exit_price = sl
                        exit_time_str = _fmt(float(sim["time"]))
                        break
                    elif tp_hit:
                        result = "TP HIT"
                        exit_price = tp
                        exit_time_str = _fmt(float(sim["time"]))
                        break

                pnl_pts = (
                    exit_price - entry if signal == "LONG" else entry - exit_price
                )

                self.trades.append(
                    {
                        "type": signal,
                        "status": "CLOSED",
                        "entry_time": entry_time_str,
                        "exit_time": exit_time_str,
                        "entry_price": entry,
                        "exit_price": exit_price,
                        "points": round(pnl_pts, 4),
                        "exit_type": result,
                        "orb_h1": h1,
                        "orb_l1": l1,
                        "sl": sl,
                        "tp": tp,
                        "entry_atr": 0.0,
                    }
                )
                break

        return self.trades

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _set_orb(self, candle, candle_date: date):
        if candle_date != self.orb_date:
            self._reset_daily_state()
        self.orb_h1 = float(candle["high"])
        self.orb_l1 = float(candle["low"])
        self.orb_close = float(candle["close"])
        self.orb_date = candle_date
        logger.info(
            f"[{self.symbol}] ORB set for {candle_date}: H1={self.orb_h1:.2f}, L1={self.orb_l1:.2f}, Close={self.orb_close:.2f}"
        )


    def _reset_daily_state(self):
        self.trade_taken_today = False
        if self.current_position == 0:
            self.entry_price = None
            self.tp_price = None
            self.sl_price = None
            self.entry_side = None
            self.trade_id = None

    def _open_trade(self, side: str, price: float, reason: str, time_str: str):
        assert self.orb_h1 is not None and self.orb_l1 is not None

        if side == "long":
            self.sl_price = self.orb_l1
            risk = max(price - self.orb_l1, price * self.pct_threshold)
            self.tp_price = price + (risk * self.rr_ratio)
            self.current_position = 1
            action = "ENTRY_LONG"
            discord_side = "LONG ENTRY"
        else:
            self.sl_price = self.orb_h1
            risk = max(self.orb_h1 - price, price * self.pct_threshold)
            self.tp_price = price - (risk * self.rr_ratio)
            self.current_position = -1
            action = "ENTRY_SHORT"
            discord_side = "SHORT ENTRY"


        self.entry_price = price
        self.entry_side = side
        self.trade_id = f"orb_{self.symbol}_{datetime.now(tz=IST).strftime('%Y%m%d_%H%M%S')}"
        self.trade_taken_today = True

        logger.info(
            f"[{self.symbol}] {action} @ {price:.2f} | "
            f"SL={self.sl_price:.2f} TP={self.tp_price:.2f} | {reason}"
        )

        try:
            config = get_config()
            mode = getattr(config, "get_mode", lambda: "live")()
            journal_orb_entry(
                trade_id=self.trade_id,
                symbol=self.symbol,
                action=action,
                side="buy" if side == "long" else "sell",
                price=price,
                order_size=self.fixed_lot_size,
                leverage=self.leverage,
                mode=mode,
                strategy_name=STRATEGY_NAME,
                reason=reason,
                orb_h1=self.orb_h1,
                orb_l1=self.orb_l1,
                tp_price=self.tp_price,
                sl_price=self.sl_price,
                entry_time_ist=time_str,
            )
        except Exception as e:
            logger.error(f"[{self.symbol}] Firestore entry journal failed: {e}")

        try:
            config = get_config()
            mode = getattr(config, "get_mode", lambda: "live")()
            webhook_url = getattr(config, "discord_webhook_url", None)
            if webhook_url:
                notifier = DiscordNotifier(webhook_url)
                notifier.send_trade_alert(
                    symbol=self.symbol,
                    side=discord_side,
                    price=price,
                    reason=reason,
                    stop_loss_price=self.sl_price,
                    take_profit_price=self.tp_price,
                    lot_size=self.fixed_lot_size,
                    strategy_name=STRATEGY_NAME,
                    timeframe=self.timeframe,
                    mode=mode,
                )
        except Exception as e:
            logger.error(f"[{self.symbol}] Discord entry alert failed: {e}")

    def _close_trade(self, price: float, reason: str, time_str: str):
        pnl = 0.0
        if self.entry_price is not None:
            if self.current_position == 1:
                pnl = price - self.entry_price
                action = "EXIT_LONG"
                discord_side = "EXIT LONG"
            else:
                pnl = self.entry_price - price
                action = "EXIT_SHORT"
                discord_side = "EXIT SHORT"
        else:
            action = "EXIT"
            discord_side = "EXIT"

        logger.info(
            f"[{self.symbol}] {action} @ {price:.2f} | PnL={pnl:+.2f} pts | {reason}"
        )

        self.current_position = 0

        try:
            journal_orb_exit(
                trade_id=self.trade_id,
                action=action,
                side="sell" if action == "EXIT_LONG" else "buy",
                exit_price=price,
                realized_pnl=pnl,
                reason=reason,
                exit_time_ist=time_str,
            )
        except Exception as e:
            logger.error(f"[{self.symbol}] Firestore exit journal failed: {e}")

        try:
            config = get_config()
            mode = getattr(config, "get_mode", lambda: "live")()
            webhook_url = getattr(config, "discord_webhook_url", None)
            if webhook_url:
                notifier = DiscordNotifier(webhook_url)
                notifier.send_trade_alert(
                    symbol=self.symbol,
                    side=discord_side,
                    price=price,
                    reason=reason,
                    pnl=pnl,
                    lot_size=self.fixed_lot_size,
                    strategy_name=STRATEGY_NAME,
                    timeframe=self.timeframe,
                    mode=mode,
                )
        except Exception as e:
            logger.error(f"[{self.symbol}] Discord exit alert failed: {e}")

        self.entry_price = None
        self.tp_price = None
        self.sl_price = None
        self.entry_side = None
        self.trade_id = None
        self._save_state()

    def _save_state(self):
        """Save strategy state to local JSON file for crash resilience."""
        import json
        state_dir = Path("logs/state")
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"gold_orb_{self.symbol}.json"

        data = {
            "orb_h1": self.orb_h1,
            "orb_l1": self.orb_l1,
            "orb_close": self.orb_close,
            "orb_date": str(self.orb_date) if self.orb_date else None,
            "trade_taken_today": self.trade_taken_today,
            "current_position": self.current_position,
            "entry_price": self.entry_price,
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "entry_side": self.entry_side,
            "trade_id": self.trade_id,
        }
        try:
            state_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save strategy state: {e}")

    def load_state(self):
        """Load strategy state from local JSON file on restart."""
        import json
        state_file = Path("logs/state") / f"gold_orb_{self.symbol}.json"
        if not state_file.exists():
            return

        try:
            data = json.loads(state_file.read_text())
            self.orb_h1 = data.get("orb_h1")
            self.orb_l1 = data.get("orb_l1")
            self.orb_close = data.get("orb_close")
            d_str = data.get("orb_date")
            self.orb_date = datetime.strptime(d_str, "%Y-%m-%d").date() if d_str else None
            self.trade_taken_today = data.get("trade_taken_today", False)
            self.current_position = data.get("current_position", 0)
            self.entry_price = data.get("entry_price")
            self.tp_price = data.get("tp_price")
            self.sl_price = data.get("sl_price")
            self.entry_side = data.get("entry_side")
            self.trade_id = data.get("trade_id")
            logger.info(f"[{self.symbol}] Restored state on restart | trade_taken_today={self.trade_taken_today}, pos={self.current_position}")
        except Exception as e:
            logger.error(f"Failed to load strategy state: {e}")

    def reconcile_on_restart(self, client, product_id: int):
        """Reconcile internal state with exchange positions & orders on service restart."""
        self.load_state()

        try:
            positions = client.get_positions(product_id=product_id)
            pos = next((p for p in positions if str(p.get("product_id")) == str(product_id)), None)
            active_size = float(pos.get("size", 0)) if pos else 0.0

            if active_size != 0:
                # Position is still OPEN on exchange
                self.current_position = 1 if active_size > 0 else -1
                self.trade_taken_today = True
                logger.info(f"[{self.symbol}] RECOVERY: Restored active position (size={active_size}) from Delta Exchange")
            elif self.current_position != 0:
                # Bot thought position was open, but exchange reports 0 (TP/SL hit while bot was offline)
                logger.info(f"[{self.symbol}] RECOVERY: Trade completed on exchange while bot was offline. Syncing state.")
                self.current_position = 0
                self._save_state()
        except Exception as e:
            logger.error(f"Failed to reconcile state with Delta Exchange: {e}")

