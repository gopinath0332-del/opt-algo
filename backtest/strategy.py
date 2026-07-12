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
    get_straddle_price,
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
        # 4 transactions: entry C, entry P, exit C, exit P
        # Fee per contract per transaction = fee_rate * spot (ATM strike as proxy)
        if self.fee_rate > 0 and self.spot_estimate:
            raw_fee = 4 * self.lot_size * self.contract_value * self.fee_rate * self.spot_estimate
            # Cap: fee_cap_pct of total premium value traded across all 4 fills
            premium_cap = (self.fee_cap_pct / 100.0) * (self.entry_premium + self.exit_premium) * self.lot_size * self.contract_value
            self.fee_usd = min(raw_fee, premium_cap)
        else:
            self.fee_usd = 0.0

        # ---- Slippage cost ------------------------------------------------
        s = self.slippage_pct / 100.0
        self.slippage_usd = (self.entry_premium + self.exit_premium) * s * self.lot_size * self.contract_value

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
        self.cfg     = cfg
        self.trades:  List[TradeResult] = []
        self.skipped: List[SkippedDay]  = []

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

        # ---- 2. Entry prices ----------------------------------------------
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
            lot_size      = cfg.lot_size,
            sl_threshold  = sl_threshold,
            spot_estimate = atm,
            fee_rate      = cfg.fee_rate,
            slippage_pct  = cfg.slippage_pct,
            contract_value = cfg.contract_value,
            fee_cap_pct   = cfg.fee_cap_pct,
        )

        log.info(
            "%s | Strike=%g | Premium %.2f->%.2f | Gross $%.2f | Fee $%.2f | Net $%.2f | %s",
            trade_date, atm, entry_premium, exit_premium,
            result.pnl_usd, result.fee_usd, result.net_pnl_usd, exit_reason,
        )

        self.trades.append(result)
        return result

    def _skip(self, trade_date: date, reason: str) -> None:
        log.warning("%s: SKIP — %s", trade_date, reason)
        self.skipped.append(SkippedDay(trade_date, reason))
