"""
backtest/strategy.py
====================
Core short-straddle backtest engine.

For each trading day:
  1. Find ATM strike at entry time (11:30 UTC)
  2. Record entry prices for call + put (VWAP in ±5 min window)
  3. Monitor ticks from entry → exit (11:55 UTC):
       - On every tick: check if combined premium loss ≥ SL threshold
       - If SL hit: record SL exit
  4. If SL not hit: time-exit at 11:55 UTC
  5. Return TradeResult for the day

P&L per trade:
  = (entry_call + entry_put - exit_call - exit_put) × lot_size
  Positive = profit (premium decayed), Negative = loss (premium expanded).
  Prices are in USD per contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, List

import pandas as pd

from .config import BacktestConfig
from .price_engine import (
    find_atm_strike,
    find_otm_strikes,
    get_straddle_price,
    get_strangle_price,
    get_tick_prices_after_entry,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    trade_date:     date
    atm_strike:     float

    entry_ts:       pd.Timestamp
    entry_call:     float
    entry_put:      float
    entry_premium:  float          # call + put at entry

    exit_ts:        pd.Timestamp
    exit_call:      float
    exit_put:       float
    exit_premium:   float          # call + put at exit (raw market prices)

    exit_reason:    str            # "time_exit" | "sl_hit" | "time_exit_fallback"
    lot_size:       int
    sl_threshold:   float          # premium level that triggers SL
    spot_estimate:  Optional[float]
    fee_rate:       float          # e.g. 0.0003 for Deribit 0.03%
    slippage_pct:   float          # e.g. 1.0 for 1%
    contract_value: float = 0.001  # e.g. 0.001 for BTC
    fee_cap_pct:    float = 10.0   # e.g. 10.0%

    # Derived (set in __post_init__)
    pnl_usd:        float = 0.0    # gross P&L (entry_prem - exit_prem) * lot_size * contract_value
    fee_usd:        float = 0.0    # trading fee (capped at fee_cap_pct of premium)
    slippage_usd:   float = 0.0    # cost of bid-ask slippage
    net_pnl_usd:    float = 0.0    # pnl_usd - fee_usd - slippage_usd

    def __post_init__(self):
        # Gross P&L (incorporating option contract size multiplier)
        self.pnl_usd = (self.entry_premium - self.exit_premium) * self.lot_size * self.contract_value

        # ---- Trading fee --------------------------------------------------
        if self.fee_rate > 0 and self.spot_estimate:
            if self.exit_reason == "sl_hit":
                # Closed early: 4 taker transactions
                raw_fee = 4 * self.lot_size * self.contract_value * self.fee_rate * self.spot_estimate
                premium_cap = (self.fee_cap_pct / 100.0) * (self.entry_premium + self.exit_premium) * self.lot_size * self.contract_value
                self.fee_usd = min(raw_fee, premium_cap)
            else:
                # Held to expiry: 2 entry taker transactions + 1 settlement transaction (for the ITM leg)
                # Entry fees:
                raw_entry_fee = 2 * self.lot_size * self.contract_value * self.fee_rate * self.spot_estimate
                entry_cap = (self.fee_cap_pct / 100.0) * self.entry_premium * self.lot_size * self.contract_value
                entry_fee = min(raw_entry_fee, entry_cap)

                # Settlement fee for ITM leg (0.01% of spot, capped at 10% of option payout)
                settle_fee_rate = 0.0001
                raw_settle_fee = self.lot_size * self.contract_value * settle_fee_rate * self.spot_estimate
                settle_cap = 0.10 * self.exit_premium * self.lot_size * self.contract_value
                settle_fee = min(raw_settle_fee, settle_cap)

                self.fee_usd = entry_fee + settle_fee
        else:
            self.fee_usd = 0.0

        # ---- Slippage cost ------------------------------------------------
        s = self.slippage_pct / 100.0
        if self.exit_reason == "sl_hit":
            # Slippage on both entry and exit
            self.slippage_usd = (self.entry_premium + self.exit_premium) * s * self.lot_size * self.contract_value
        else:
            # Held to expiry: slippage on entry only!
            self.slippage_usd = self.entry_premium * s * self.lot_size * self.contract_value

        self.net_pnl_usd = self.pnl_usd - self.fee_usd - self.slippage_usd


@dataclass
class SkippedDay:
    trade_date: date
    reason:     str


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ShortStraddleEngine:
    """Run the short-straddle strategy day by day."""

    def __init__(self, cfg: BacktestConfig):
        self.cfg      = cfg
        self.trades:  List[TradeResult] = []
        self.skipped: List[SkippedDay]  = []
        self._equity: float = cfg.initial_capital   # tracks running balance

    def _compute_lot_size(self, spot: float) -> int:
        """
        Compute per-day lot size using the same leverage-based margin formula as the live bot.

            lot_size = floor(equity × alloc_pct / (2 × spot × contract_value / leverage))

        Capped at cfg.max_lot_size to prevent unrealistic compounding blow-up.
        Falls back to cfg.lot_size when dynamic sizing is disabled or inputs are invalid.
        """
        if not self.cfg.use_dynamic_lot_size:
            return self.cfg.lot_size
        try:
            capital        = self._equity * (self.cfg.capital_allocation_pct / 100.0)
            margin_per_lot = 2 * spot * self.cfg.contract_value / self.cfg.leverage
            if margin_per_lot <= 0:
                return self.cfg.lot_size
            computed = max(1, int(capital / margin_per_lot))
            if self.cfg.max_lot_size > 0:
                computed = min(computed, self.cfg.max_lot_size)
            log.debug(
                "Dynamic lot size: equity=$%.2f, capital=$%.2f, spot=$%.0f, "
                "margin/lot=$%.4f -> lots=%d%s",
                self._equity, capital, spot, margin_per_lot, computed,
                " (capped)" if self.cfg.max_lot_size > 0 and computed == self.cfg.max_lot_size else "",
            )
            return computed
        except Exception:
            return self.cfg.lot_size

    def run_day(self, trade_date: date, day_df: pd.DataFrame) -> Optional[TradeResult]:
        """Execute strategy for one day. Returns TradeResult or None if skipped."""
        cfg = self.cfg

        # ---- 1. ATM strike at entry time ----------------------------------
        atm = find_atm_strike(
            day_df, trade_date,
            cfg.entry_time_utc,
            cfg.price_window_minutes,
        )
        if atm is None:
            self._skip(trade_date, "no ATM strike found at entry time")
            return None

        # ---- 2. Dynamic lot size ------------------------------------------
        lot_size = self._compute_lot_size(spot=atm)

        # ---- 3. Entry prices -----------------------------------------------
        entry_call, entry_put = get_straddle_price(
            day_df, atm, trade_date,
            cfg.entry_time_utc,
            cfg.price_window_minutes,
        )
        if entry_call is None or entry_put is None:
            self._skip(trade_date, f"missing entry price (C={entry_call}, P={entry_put})")
            return None

        entry_premium = entry_call + entry_put
        if entry_premium <= 0:
            self._skip(trade_date, "zero entry premium")
            return None

        # SL fires when combined exit premium ≥ entry_premium × (1 + sl_pct/100)
        sl_threshold = entry_premium * (1.0 + cfg.sl_pct / 100.0)

        entry_ts = pd.Timestamp(datetime.combine(trade_date, cfg.entry_time_utc))
        exit_ts  = pd.Timestamp(datetime.combine(trade_date, cfg.exit_time_utc))

        log.debug(
            "%s | ATM=%g | C=%.2f P=%.2f | Premium=%.2f | SL@%.2f",
            trade_date, atm, entry_call, entry_put, entry_premium, sl_threshold,
        )

        # ---- 3. Tick-level SL monitoring ----------------------------------
        exit_call     = None
        exit_put      = None
        exit_reason   = "time_exit"
        actual_exit_ts = exit_ts

        tick_df = get_tick_prices_after_entry(
            day_df,
            call_strike=atm,
            put_strike=atm,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_call=entry_call,
            entry_put=entry_put,
        )

        if cfg.sl_mode in ("minute", "1min") and not tick_df.empty:
            tick_df = (
                tick_df.set_index("ts")
                .resample("1Min")
                .last()
                .ffill()
                .reset_index()
            )

        for _, row in tick_df.iterrows():
            combined = row["call_price"] + row["put_price"]
            if combined >= sl_threshold:
                exit_call      = float(row["call_price"])
                exit_put       = float(row["put_price"])
                exit_reason    = "sl_hit"
                actual_exit_ts = row["ts"]
                log.debug(
                    "%s | SL HIT @ %s | combined=%.2f",
                    trade_date, actual_exit_ts, combined,
                )
                break

        # ---- 4. Time exit (if SL not hit) ---------------------------------
        if exit_call is None:
            exit_call, exit_put = get_straddle_price(
                day_df, atm, trade_date,
                cfg.exit_time_utc,
                cfg.price_window_minutes,
            )

        # Fallback: last tick of each leg before exit_ts
        if exit_call is None or exit_put is None:
            fallback_c, fallback_p = None, None
            for ot, leg in [("C", "c"), ("P", "p")]:
                mask = (
                    (day_df["opt_type"] == ot) &
                    (day_df["strike"]   == atm) &
                    (day_df["ts"]       <= exit_ts)
                )
                s = day_df[mask]
                if not s.empty:
                    if leg == "c":
                        fallback_c = float(s.iloc[-1]["price"])
                    else:
                        fallback_p = float(s.iloc[-1]["price"])

            if fallback_c is None or fallback_p is None:
                self._skip(trade_date, "no exit price data")
                return None
            exit_call   = fallback_c
            exit_put    = fallback_p
            exit_reason = "time_exit_fallback"

        exit_premium = exit_call + exit_put

        # ---- 5. Build result ----------------------------------------------
        result = TradeResult(
            trade_date    = trade_date,
            atm_strike    = atm,
            entry_ts      = entry_ts,
            entry_call    = entry_call,
            entry_put     = entry_put,
            entry_premium = entry_premium,
            exit_ts       = actual_exit_ts,
            exit_call     = exit_call,
            exit_put      = exit_put,
            exit_premium  = exit_premium,
            exit_reason   = exit_reason,
            lot_size      = lot_size,
            sl_threshold  = sl_threshold,
            spot_estimate = atm,
            fee_rate      = cfg.fee_rate,
            slippage_pct  = cfg.slippage_pct,
            contract_value = cfg.contract_value,
            fee_cap_pct   = cfg.fee_cap_pct,
        )

        log.info(
            "%s | Strike=%g | Premium %.2f->%.2f | Gross $%.2f | Fee $%.2f | Net $%.2f | %s | lots=%d",
            trade_date, atm, entry_premium, exit_premium,
            result.pnl_usd, result.fee_usd, result.net_pnl_usd, exit_reason, lot_size,
        )

        self.trades.append(result)
        self._equity += result.net_pnl_usd   # update running equity
        return result

    def _skip(self, trade_date: date, reason: str) -> None:
        log.warning("%s: SKIP — %s", trade_date, reason)
        self.skipped.append(SkippedDay(trade_date, reason))


# ---------------------------------------------------------------------------
# OTM Strangle Engine
# ---------------------------------------------------------------------------

@dataclass
class StrangleTradeResult(TradeResult):
    """Extends TradeResult with OTM-specific fields."""
    call_strike: float = 0.0
    put_strike:  float = 0.0
    otm_steps:   int   = 0


class ShortStrangleEngine:
    """
    Short OTM strangle backtest engine.

    Identical to ShortStraddleEngine except:
    - Calls  are sold N steps above ATM  (call_strike = atm + N × $200)
    - Puts   are sold N steps below ATM  (put_strike  = atm - N × $200)

    All other mechanics (entry VWAP, SL monitoring, fees, slippage,
    TradeResult dataclass) are identical and reused.
    """

    def __init__(self, cfg: BacktestConfig):
        self.cfg       = cfg
        self.otm_steps = cfg.otm_steps
        self.trades:  List[TradeResult] = []
        self.skipped: List[SkippedDay]  = []
        self._equity: float = cfg.initial_capital   # tracks running balance

    def _compute_lot_size(self, spot: float) -> int:
        """Same margin-based formula as the live bot and ShortStraddleEngine."""
        if not self.cfg.use_dynamic_lot_size:
            return self.cfg.lot_size
        try:
            capital        = self._equity * (self.cfg.capital_allocation_pct / 100.0)
            margin_per_lot = 2 * spot * self.cfg.contract_value / self.cfg.leverage
            if margin_per_lot <= 0:
                return self.cfg.lot_size
            computed = max(1, int(capital / margin_per_lot))
            if self.cfg.max_lot_size > 0:
                computed = min(computed, self.cfg.max_lot_size)
            return computed
        except Exception:
            return self.cfg.lot_size

    def run_day(self, trade_date: date, day_df: pd.DataFrame) -> Optional[TradeResult]:
        """Execute OTM strangle for one day. Returns TradeResult or None if skipped."""
        cfg = self.cfg

        # ---- 1. Find ATM strike -------------------------------------------
        atm = find_atm_strike(
            day_df, trade_date,
            cfg.entry_time_utc,
            cfg.price_window_minutes,
        )
        if atm is None:
            self._skip(trade_date, "no ATM strike found at entry time")
            return None

        # ---- 2. Find OTM strikes ------------------------------------------
        call_strike, put_strike = find_otm_strikes(day_df, atm, self.otm_steps)
        if call_strike is None:
            self._skip(trade_date, f"OTM call strike ({atm + self.otm_steps * 200}) not in data")
            return None
        if put_strike is None:
            self._skip(trade_date, f"OTM put strike ({atm - self.otm_steps * 200}) not in data")
            return None

        # ---- 3. Dynamic lot size ------------------------------------------
        lot_size = self._compute_lot_size(spot=atm)

        # ---- 4. Entry prices -----------------------------------------------
        entry_call, entry_put = get_strangle_price(
            day_df, call_strike, put_strike, trade_date,
            cfg.entry_time_utc, cfg.price_window_minutes,
        )
        if entry_call is None or entry_put is None:
            self._skip(trade_date, f"missing entry price (C={entry_call}, P={entry_put})")
            return None

        entry_premium = entry_call + entry_put
        if entry_premium <= 0:
            self._skip(trade_date, "zero entry premium")
            return None

        sl_threshold = entry_premium * (1.0 + cfg.sl_pct / 100.0)

        entry_ts = pd.Timestamp(datetime.combine(trade_date, cfg.entry_time_utc))
        exit_ts  = pd.Timestamp(datetime.combine(trade_date, cfg.exit_time_utc))

        log.debug(
            "%s | ATM=%g | C_strike=%g P_strike=%g | C=%.2f P=%.2f | Premium=%.2f | SL@%.2f",
            trade_date, atm, call_strike, put_strike,
            entry_call, entry_put, entry_premium, sl_threshold,
        )

        # ---- 4. Tick-level SL monitoring -----------------------------------
        exit_call   = None
        exit_put    = None
        exit_reason = "time_exit"
        actual_exit_ts = exit_ts

        tick_df = get_tick_prices_after_entry(
            day_df,
            call_strike=call_strike,
            put_strike=put_strike,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_call=entry_call,
            entry_put=entry_put,
        )

        if cfg.sl_mode in ("minute", "1min") and not tick_df.empty:
            tick_df = (
                tick_df.set_index("ts")
                .resample("1Min")
                .last()
                .ffill()
                .reset_index()
            )

        for _, row in tick_df.iterrows():
            combined = row["call_price"] + row["put_price"]
            if combined >= sl_threshold:
                exit_call      = float(row["call_price"])
                exit_put       = float(row["put_price"])
                exit_reason    = "sl_hit"
                actual_exit_ts = row["ts"]
                log.debug("%s | SL HIT @ %s | combined=%.2f", trade_date, actual_exit_ts, combined)
                break

        # ---- 5. Time exit (if SL not hit) ----------------------------------
        if exit_call is None:
            exit_call, exit_put = get_strangle_price(
                day_df, call_strike, put_strike, trade_date,
                cfg.exit_time_utc, cfg.price_window_minutes,
            )

        # Fallback: last tick before exit_ts for each leg
        if exit_call is None or exit_put is None:
            fallback_c, fallback_p = None, None
            for ot, strike, leg in [("C", call_strike, "c"), ("P", put_strike, "p")]:
                mask = (
                    (day_df["opt_type"] == ot) &
                    (day_df["strike"]   == strike) &
                    (day_df["ts"]       <= exit_ts)
                )
                s = day_df[mask]
                if not s.empty:
                    if leg == "c":
                        fallback_c = float(s.iloc[-1]["price"])
                    else:
                        fallback_p = float(s.iloc[-1]["price"])

            if fallback_c is None or fallback_p is None:
                self._skip(trade_date, "no exit price data")
                return None
            exit_call   = fallback_c
            exit_put    = fallback_p
            exit_reason = "time_exit_fallback"

        exit_premium = exit_call + exit_put

        # ---- 6. Build result -----------------------------------------------
        result = TradeResult(
            trade_date    = trade_date,
            atm_strike    = atm,
            entry_ts      = entry_ts,
            entry_call    = entry_call,
            entry_put     = entry_put,
            entry_premium = entry_premium,
            exit_ts       = actual_exit_ts,
            exit_call     = exit_call,
            exit_put      = exit_put,
            exit_premium  = exit_premium,
            exit_reason   = exit_reason,
            lot_size      = lot_size,
            sl_threshold  = sl_threshold,
            spot_estimate = atm,
            fee_rate      = cfg.fee_rate,
            slippage_pct  = cfg.slippage_pct,
            contract_value = cfg.contract_value,
            fee_cap_pct   = cfg.fee_cap_pct,
        )

        log.info(
            "%s | C=%g(+%d) P=%g(-%d) | Prem %.2f->%.2f | Gross $%.2f | Net $%.2f | %s | lots=%d",
            trade_date,
            call_strike, self.otm_steps,
            put_strike,  self.otm_steps,
            entry_premium, exit_premium,
            result.pnl_usd, result.net_pnl_usd, exit_reason, lot_size,
        )

        self.trades.append(result)
        self._equity += result.net_pnl_usd   # update running equity
        return result

    def _skip(self, trade_date: date, reason: str) -> None:
        log.warning("%s: SKIP (strangle) — %s", trade_date, reason)
        self.skipped.append(SkippedDay(trade_date, reason))
