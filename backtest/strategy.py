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


# ---------------------------------------------------------------------------
# Momentum-Triggered Straddle Engine
# ---------------------------------------------------------------------------

@dataclass
class MomentumTradeResult(TradeResult):
    """Extends TradeResult with momentum-trigger specific fields."""
    trigger_ts:        Optional[pd.Timestamp] = None   # When the 0.5% move threshold was hit
    trigger_move_pct:  float = 0.0                     # % move that fired the entry
    baseline_price:    float = 0.0                     # BTC spot snapshotted at session open
    sl_hit_leg:        Optional[str] = None            # "CE", "PE", or None (time exit)


class MomentumStraddleEngine:
    """
    Momentum-triggered ATM short straddle backtest engine.

    Faithfully simulates the btc_momentum_straddle live strategy:

    1. Snapshot baseline: find ATM strike at entry_time_utc (16:00 IST = 10:30 UTC).
       This is used as the session-open spot proxy (baseline_price).

    2. Scan ticks from entry_time_utc → exit_time_utc at 1-min resolution.
       Find the first minute where:
           |atm_proxy - baseline_price| / baseline_price * 100 >= momentum_threshold_pct
       That minute's timestamp becomes the actual entry time.

    3. If no trigger: SkippedDay("no momentum trigger").

    4. Entry: VWAP call + put around the trigger timestamp (±price_window_minutes).

    5. Per-leg SL: iterate ticks after entry.
       - CE SL fires when current_call >= call_entry * (1 + sl_pct/100)
       - PE SL fires when current_put  >= put_entry  * (1 + sl_pct/100)
       - Either hit → record which leg hit, close both, exit_reason = "sl_hit"

    6. Time exit at exit_time_utc if SL not triggered.

    P&L calculation is identical to ShortStraddleEngine (inherited via TradeResult).
    """

    def __init__(self, cfg: BacktestConfig):
        self.cfg      = cfg
        self.trades:  List[MomentumTradeResult] = []
        self.skipped: List[SkippedDay] = []
        self._equity: float = cfg.initial_capital

    def _compute_lot_size(self, spot: float) -> int:
        """Same margin-based formula as ShortStraddleEngine and the live bot."""
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

    def run_day(self, trade_date: date, day_df: pd.DataFrame) -> Optional[MomentumTradeResult]:
        """Execute momentum straddle for one day. Returns MomentumTradeResult or None if skipped."""
        cfg = self.cfg

        entry_ts_anchor = pd.Timestamp(datetime.combine(trade_date, cfg.entry_time_utc))
        exit_ts         = pd.Timestamp(datetime.combine(trade_date, cfg.exit_time_utc))

        # ── Step 1: Baseline — ATM strike at session open (entry_time_utc) ──────
        baseline_atm = find_atm_strike(
            day_df, trade_date,
            cfg.entry_time_utc,
            cfg.price_window_minutes,
        )
        if baseline_atm is None:
            self._skip(trade_date, "no ATM strike found at session open (baseline)")
            return None
        baseline_price = baseline_atm   # use ATM strike as spot proxy

        # ── Step 2: Scan ticks for momentum trigger ───────────────────────────
        # Build 1-min ATM proxy from available C ticks (call price approximates spot movement)
        atm_mask = (
            (day_df["opt_type"] == "C") &
            (day_df["strike"]   == baseline_atm) &
            (day_df["ts"]       >= entry_ts_anchor) &
            (day_df["ts"]       <= exit_ts)
        )
        atm_ticks = day_df[atm_mask][["ts", "price"]].copy()

        # For the momentum proxy we use the mid-price of call+put to approximate spot movement.
        # Both C and P at ATM react to spot moves; tracking ATM call_price tracks momentum direction.
        # More accurately: use (C_price - P_price) parity to get delta from baseline.
        # Simplest correct approach: track ATM VWAP per minute for both legs and use
        # (call_price - put_price) / baseline_price as a signed spot-equivalent move.

        # Get minute-level C and P prices after entry_ts_anchor
        cp_mask = (
            (
                ((day_df["opt_type"] == "C") & (day_df["strike"] == baseline_atm)) |
                ((day_df["opt_type"] == "P") & (day_df["strike"] == baseline_atm))
            ) &
            (day_df["ts"] >= entry_ts_anchor) &
            (day_df["ts"] <= exit_ts)
        )
        cp_ticks = day_df[cp_mask].copy()

        trigger_ts      = None
        trigger_move_pct = 0.0

        if not cp_ticks.empty:
            # Resample to 1-min VWAP for each leg, forward-fill
            c_ticks = (
                cp_ticks[cp_ticks["opt_type"] == "C"][["ts", "price"]]
                .set_index("ts")
                .resample("1Min").last().ffill()
                .rename(columns={"price": "call_price"})
            )
            p_ticks = (
                cp_ticks[cp_ticks["opt_type"] == "P"][["ts", "price"]]
                .set_index("ts")
                .resample("1Min").last().ffill()
                .rename(columns={"price": "put_price"})
            )
            minute_df = c_ticks.join(p_ticks, how="outer").ffill().dropna()

            # Put-call parity: spot ≈ strike + call_price - put_price
            # Spot move from baseline ≈ (call_price - put_price) - (baseline_call - baseline_put)
            # Since at entry_ts baseline C≈P (ATM), baseline_call - baseline_put ≈ 0
            # Simplified: spot_proxy = baseline_atm + (call_price - put_price)
            minute_df["spot_proxy"]  = baseline_atm + (minute_df["call_price"] - minute_df["put_price"])
            minute_df["move_pct"]    = (
                (minute_df["spot_proxy"] - baseline_price).abs() / baseline_price * 100
            )

            # Find first minute where move >= threshold (skip the very first row = baseline)
            triggered = minute_df.iloc[1:][
                minute_df.iloc[1:]["move_pct"] >= cfg.momentum_threshold_pct
            ]
            if not triggered.empty:
                trigger_ts       = triggered.index[0]
                trigger_move_pct = float(triggered.iloc[0]["move_pct"])
                log.debug(
                    "%s | Momentum FIRED @ %s | move=%.3f%% (baseline ATM=$%.0f)",
                    trade_date, trigger_ts, trigger_move_pct, baseline_price,
                )

        if trigger_ts is None:
            self._skip(
                trade_date,
                f"no momentum trigger (threshold={cfg.momentum_threshold_pct}%)"
            )
            return None

        # ── Step 3: Dynamic lot size ──────────────────────────────────────────
        lot_size = self._compute_lot_size(spot=baseline_atm)

        # ── Step 4: Entry prices at trigger timestamp ─────────────────────────
        trigger_time = trigger_ts.time()
        entry_call, entry_put = get_straddle_price(
            day_df, baseline_atm, trade_date,
            trigger_time,
            cfg.price_window_minutes,
        )
        if entry_call is None or entry_put is None:
            self._skip(trade_date, f"missing entry price at trigger time {trigger_ts} (C={entry_call}, P={entry_put})")
            return None

        entry_premium = entry_call + entry_put
        if entry_premium <= 0:
            self._skip(trade_date, "zero entry premium at trigger")
            return None

        # Per-leg SL thresholds: mark must double (100%)
        call_sl_threshold = entry_call * (1.0 + cfg.sl_pct / 100.0)
        put_sl_threshold  = entry_put  * (1.0 + cfg.sl_pct / 100.0)
        # Combined reference (for TradeResult.sl_threshold field)
        combined_sl_threshold = entry_premium * (1.0 + cfg.sl_pct / 100.0)

        log.debug(
            "%s | Trigger=%s | move=%.3f%% | ATM=%g | C=%.4f P=%.4f | "
            "Premium=%.4f | CE_SL@%.4f | PE_SL@%.4f",
            trade_date, trigger_ts, trigger_move_pct, baseline_atm,
            entry_call, entry_put, entry_premium,
            call_sl_threshold, put_sl_threshold,
        )

        # ── Step 5: Tick-level per-leg SL monitoring ──────────────────────────
        exit_call      = None
        exit_put       = None
        exit_reason    = "time_exit"
        actual_exit_ts = exit_ts
        sl_hit_leg     = None

        tick_df = get_tick_prices_after_entry(
            day_df,
            call_strike=baseline_atm,
            put_strike=baseline_atm,
            entry_ts=trigger_ts,
            exit_ts=exit_ts,
            entry_call=entry_call,
            entry_put=entry_put,
        )

        # Resample to 1-min for per-leg SL check (filters execution noise)
        if not tick_df.empty:
            tick_df = (
                tick_df.set_index("ts")
                .resample("1Min").last().ffill()
                .reset_index()
            )

        for _, row in tick_df.iterrows():
            c_mark = row["call_price"]
            p_mark = row["put_price"]

            if cfg.sl_per_leg:
                # Per-leg independent check
                if c_mark >= call_sl_threshold:
                    exit_call      = float(c_mark)
                    exit_put       = float(p_mark)
                    exit_reason    = "sl_hit"
                    actual_exit_ts = row["ts"]
                    sl_hit_leg     = "CE"
                    log.debug("%s | PER-LEG SL HIT CE @ %s | C=%.4f (SL@%.4f)",
                              trade_date, actual_exit_ts, c_mark, call_sl_threshold)
                    break
                elif p_mark >= put_sl_threshold:
                    exit_call      = float(c_mark)
                    exit_put       = float(p_mark)
                    exit_reason    = "sl_hit"
                    actual_exit_ts = row["ts"]
                    sl_hit_leg     = "PE"
                    log.debug("%s | PER-LEG SL HIT PE @ %s | P=%.4f (SL@%.4f)",
                              trade_date, actual_exit_ts, p_mark, put_sl_threshold)
                    break
            else:
                # Combined SL fallback
                combined = c_mark + p_mark
                if combined >= combined_sl_threshold:
                    exit_call      = float(c_mark)
                    exit_put       = float(p_mark)
                    exit_reason    = "sl_hit"
                    actual_exit_ts = row["ts"]
                    log.debug("%s | COMBINED SL HIT @ %s | combined=%.4f",
                              trade_date, actual_exit_ts, combined)
                    break

        # ── Step 6: Time exit if SL not hit ───────────────────────────────────
        if exit_call is None:
            exit_call, exit_put = get_straddle_price(
                day_df, baseline_atm, trade_date,
                cfg.exit_time_utc,
                cfg.price_window_minutes,
            )

        # Fallback: last tick before exit_ts
        if exit_call is None or exit_put is None:
            fallback_c, fallback_p = None, None
            for ot, leg in [("C", "c"), ("P", "p")]:
                mask = (
                    (day_df["opt_type"] == ot) &
                    (day_df["strike"]   == baseline_atm) &
                    (day_df["ts"]       <= exit_ts)
                )
                s = day_df[mask]
                if not s.empty:
                    if leg == "c":
                        fallback_c = float(s.iloc[-1]["price"])
                    else:
                        fallback_p = float(s.iloc[-1]["price"])

            if fallback_c is None or fallback_p is None:
                self._skip(trade_date, "no exit price data after momentum trigger")
                return None
            exit_call   = fallback_c
            exit_put    = fallback_p
            exit_reason = "time_exit_fallback"

        exit_premium = exit_call + exit_put

        # ── Step 7: Build result ───────────────────────────────────────────────
        result = MomentumTradeResult(
            trade_date     = trade_date,
            atm_strike     = baseline_atm,
            entry_ts       = trigger_ts,
            entry_call     = entry_call,
            entry_put      = entry_put,
            entry_premium  = entry_premium,
            exit_ts        = actual_exit_ts,
            exit_call      = exit_call,
            exit_put       = exit_put,
            exit_premium   = exit_premium,
            exit_reason    = exit_reason,
            lot_size       = lot_size,
            sl_threshold   = combined_sl_threshold,
            spot_estimate  = baseline_atm,
            fee_rate       = cfg.fee_rate,
            slippage_pct   = cfg.slippage_pct,
            contract_value = cfg.contract_value,
            fee_cap_pct    = cfg.fee_cap_pct,
            trigger_ts     = trigger_ts,
            trigger_move_pct = trigger_move_pct,
            baseline_price = baseline_price,
            sl_hit_leg     = sl_hit_leg,
        )

        log.info(
            "%s | ATM=%g | Trigger@%s (+%.2f%%) | Entry C=%.4f P=%.4f | "
            "Exit %.4f->%.4f | Gross $%.2f | Net $%.2f | %s%s | lots=%d",
            trade_date, baseline_atm,
            trigger_ts.strftime("%H:%M") if trigger_ts else "N/A",
            trigger_move_pct,
            entry_call, entry_put,
            entry_premium, exit_premium,
            result.pnl_usd, result.net_pnl_usd, exit_reason,
            f" [{sl_hit_leg}]" if sl_hit_leg else "",
            lot_size,
        )

        self.trades.append(result)
        self._equity += result.net_pnl_usd
        return result

    def _skip(self, trade_date: date, reason: str) -> None:
        log.warning("%s: SKIP (momentum) — %s", trade_date, reason)
        self.skipped.append(SkippedDay(trade_date, reason))


# ---------------------------------------------------------------------------
# Momentum-Triggered Directional Option Buyer Engine
# ---------------------------------------------------------------------------

@dataclass
class MomentumBuyerTradeResult(TradeResult):
    """
    Trade result for Momentum Option Buying:
    - Long CE when BTC moves >= +threshold%
    - Long PE when BTC moves <= -threshold%
    """
    direction:         str = ""                        # "UP" | "DOWN"
    leg_bought:        str = ""                        # "CE" | "PE"
    strike_bought:     float = 0.0
    trigger_ts:        Optional[pd.Timestamp] = None
    trigger_move_pct:  float = 0.0
    baseline_price:    float = 0.0
    settlement_spot:   float = 0.0

    def __post_init__(self):
        # Long Option Gross P&L: (exit_premium - entry_premium) * lot_size * contract_value
        self.pnl_usd = (self.exit_premium - self.entry_premium) * self.lot_size * self.contract_value

        # Entry taker fee: 0.03% of spot, capped at fee_cap_pct of entry premium
        if self.fee_rate > 0 and self.spot_estimate:
            raw_entry_fee = self.lot_size * self.contract_value * self.fee_rate * self.spot_estimate
            entry_cap = (self.fee_cap_pct / 100.0) * self.entry_premium * self.lot_size * self.contract_value
            entry_fee = min(raw_entry_fee, entry_cap)

            # Settlement fee for ITM leg on expiry (0.01% of spot, capped at 10% of payout)
            settle_fee = 0.0
            if self.exit_premium > 0:
                raw_settle_fee = self.lot_size * self.contract_value * 0.0001 * (self.settlement_spot or self.spot_estimate)
                settle_cap = 0.10 * self.exit_premium * self.lot_size * self.contract_value
                settle_fee = min(raw_settle_fee, settle_cap)

            self.fee_usd = entry_fee + settle_fee
        else:
            self.fee_usd = 0.0

        # Slippage: paid on entry (at market order). Expiry settlement has 0 slippage.
        s = self.slippage_pct / 100.0
        self.slippage_usd = self.entry_premium * s * self.lot_size * self.contract_value

        self.net_pnl_usd = self.pnl_usd - self.fee_usd - self.slippage_usd


class MomentumBuyerEngine:
    """
    Momentum-triggered Directional Option Buyer Engine.

    Rules:
    1. Baseline: Snapshot ATM strike at 16:00 IST (10:30 UTC).
    2. Scan minute ticks from 10:30 UTC to 12:00 UTC.
       Calculate spot move % from baseline.
       - If move >= +threshold%: Trigger UP   -> Buy ATM CE
       - If move <= -threshold%: Trigger DOWN -> Buy ATM PE
    3. Strike: ATM strike at trigger time.
    4. Sizing: Based on capital allocation (cost = entry_premium * contract_value).
    5. Exit: Hold to 17:30 IST (12:00 UTC) expiry / settlement.
    """

    def __init__(self, cfg: BacktestConfig):
        self.cfg      = cfg
        self.trades:  List[MomentumBuyerTradeResult] = []
        self.skipped: List[SkippedDay] = []
        self._equity: float = cfg.initial_capital

    def _compute_lot_size(self, entry_price: float) -> int:
        """Dynamic lot sizing for option buyer: capital / (entry_price * contract_value)."""
        if not self.cfg.use_dynamic_lot_size:
            return self.cfg.lot_size
        try:
            capital = self._equity * (self.cfg.capital_allocation_pct / 100.0)
            cost_per_contract = entry_price * self.cfg.contract_value
            if cost_per_contract <= 0:
                return self.cfg.lot_size
            computed = max(1, int(capital / cost_per_contract))
            if self.cfg.max_lot_size > 0:
                computed = min(computed, self.cfg.max_lot_size)
            return computed
        except Exception:
            return self.cfg.lot_size

    def run_day(self, trade_date: date, day_df: pd.DataFrame) -> Optional[MomentumBuyerTradeResult]:
        cfg = self.cfg

        entry_ts_anchor = pd.Timestamp(datetime.combine(trade_date, cfg.entry_time_utc))
        exit_ts         = pd.Timestamp(datetime.combine(trade_date, cfg.exit_time_utc))

        # ── Step 1: Baseline — ATM strike at session open (10:30 UTC) ──────
        baseline_atm = find_atm_strike(
            day_df, trade_date,
            cfg.entry_time_utc,
            cfg.price_window_minutes,
        )
        if baseline_atm is None:
            self._skip(trade_date, "no ATM strike found at session open (baseline)")
            return None
        baseline_price = baseline_atm

        # ── Step 2: Scan ticks for directional momentum trigger ─────────────
        cp_mask = (
            (
                ((day_df["opt_type"] == "C") & (day_df["strike"] == baseline_atm)) |
                ((day_df["opt_type"] == "P") & (day_df["strike"] == baseline_atm))
            ) &
            (day_df["ts"] >= entry_ts_anchor) &
            (day_df["ts"] <= exit_ts)
        )
        cp_ticks = day_df[cp_mask].copy()

        trigger_ts       = None
        trigger_move_pct = 0.0
        direction        = None
        leg_bought       = None

        if not cp_ticks.empty:
            c_ticks = (
                cp_ticks[cp_ticks["opt_type"] == "C"][["ts", "price"]]
                .set_index("ts")
                .resample("1Min").last().ffill()
                .rename(columns={"price": "call_price"})
            )
            p_ticks = (
                cp_ticks[cp_ticks["opt_type"] == "P"][["ts", "price"]]
                .set_index("ts")
                .resample("1Min").last().ffill()
                .rename(columns={"price": "put_price"})
            )
            minute_df = c_ticks.join(p_ticks, how="outer").ffill().dropna()

            # Signed spot proxy and signed move %
            minute_df["spot_proxy"] = baseline_atm + (minute_df["call_price"] - minute_df["put_price"])
            minute_df["move_pct"]   = (minute_df["spot_proxy"] - baseline_price) / baseline_price * 100

            # Find first trigger (skip row 0 = baseline)
            for ts, row in minute_df.iloc[1:].iterrows():
                move = row["move_pct"]
                if move >= cfg.momentum_threshold_pct:
                    trigger_ts       = ts
                    trigger_move_pct = float(move)
                    direction        = "UP"
                    leg_bought       = "CE"
                    break
                elif move <= -cfg.momentum_threshold_pct:
                    trigger_ts       = ts
                    trigger_move_pct = float(move)
                    direction        = "DOWN"
                    leg_bought       = "PE"
                    break

        if trigger_ts is None:
            self._skip(trade_date, f"no momentum trigger (threshold={cfg.momentum_threshold_pct}%)")
            return None

        # ── Step 3: Find ATM strike at trigger time ─────────────────────────
        trigger_time = trigger_ts.time()
        atm_at_trigger = find_atm_strike(
            day_df, trade_date,
            trigger_time,
            cfg.price_window_minutes,
        )
        strike_to_buy = atm_at_trigger if atm_at_trigger is not None else baseline_atm
        opt_type = "C" if leg_bought == "CE" else "P"

        # ── Step 4: Entry price of the chosen option ─────────────────────────
        from .price_engine import get_price_at_time
        entry_price = get_price_at_time(
            day_df, opt_type, strike_to_buy,
            trade_date, trigger_time,
            cfg.price_window_minutes,
        )

        if entry_price is None or entry_price <= 0:
            # Fallback: check closest tick around trigger
            mask = (
                (day_df["opt_type"] == opt_type) &
                (day_df["strike"]   == strike_to_buy) &
                (day_df["ts"]       <= trigger_ts + pd.Timedelta(minutes=cfg.price_window_minutes)) &
                (day_df["ts"]       >= trigger_ts - pd.Timedelta(minutes=cfg.price_window_minutes))
            )
            s = day_df[mask]
            if not s.empty:
                entry_price = float(s.iloc[-1]["price"])
            else:
                self._skip(trade_date, f"missing entry price for {leg_bought} {strike_to_buy} at {trigger_ts}")
                return None

        # ── Step 5: Lot size ────────────────────────────────────────────────
        lot_size = self._compute_lot_size(entry_price=entry_price)

        # ── Step 6: Settlement at 17:30 IST (12:00 UTC) expiry ─────────────
        settlement_spot = find_atm_strike(
            day_df, trade_date,
            cfg.exit_time_utc,
            cfg.price_window_minutes,
        )
        if settlement_spot is None:
            # Fallback to last known spot proxy
            settlement_spot = baseline_price * (1.0 + trigger_move_pct / 100.0)

        # Theoretical intrinsic settlement payout
        if leg_bought == "CE":
            intrinsic = max(0.0, settlement_spot - strike_to_buy)
        else:
            intrinsic = max(0.0, strike_to_buy - settlement_spot)

        # Market price near exit time (if available)
        mkt_exit = get_price_at_time(
            day_df, opt_type, strike_to_buy,
            trade_date, cfg.exit_time_utc,
            cfg.price_window_minutes,
        )

        # In case option expired OTM, market trades rarely happen at 12:00 UTC; intrinsic is exact.
        # If market trade happened very close to expiry, use market exit, otherwise intrinsic.
        exit_price = mkt_exit if mkt_exit is not None else intrinsic

        # ── Step 7: Build result ────────────────────────────────────────────
        entry_c = entry_price if leg_bought == "CE" else 0.0
        entry_p = entry_price if leg_bought == "PE" else 0.0
        exit_c  = exit_price  if leg_bought == "CE" else 0.0
        exit_p  = exit_price  if leg_bought == "PE" else 0.0

        result = MomentumBuyerTradeResult(
            trade_date       = trade_date,
            atm_strike       = strike_to_buy,
            entry_ts         = trigger_ts,
            entry_call       = entry_c,
            entry_put        = entry_p,
            entry_premium    = entry_price,
            exit_ts          = exit_ts,
            exit_call        = exit_c,
            exit_put         = exit_p,
            exit_premium     = exit_price,
            exit_reason      = "expiry_settlement",
            lot_size         = lot_size,
            sl_threshold     = 0.0,
            spot_estimate    = strike_to_buy,
            fee_rate         = cfg.fee_rate,
            slippage_pct     = cfg.slippage_pct,
            contract_value   = cfg.contract_value,
            fee_cap_pct      = cfg.fee_cap_pct,
            direction        = direction,
            leg_bought       = leg_bought,
            strike_bought    = strike_to_buy,
            trigger_ts       = trigger_ts,
            trigger_move_pct = trigger_move_pct,
            baseline_price   = baseline_price,
            settlement_spot  = settlement_spot,
        )

        log.info(
            "%s | Trigger %s (%s) @ %s (move %+.2f%%) | Strike %g | "
            "Entry $%.2f -> Exit $%.2f | P&L $%.2f (Net $%.2f) | lots=%d",
            trade_date, direction, leg_bought,
            trigger_ts.strftime("%H:%M") if trigger_ts else "N/A",
            trigger_move_pct, strike_to_buy,
            entry_price, exit_price,
            result.pnl_usd, result.net_pnl_usd,
            lot_size,
        )

        self.trades.append(result)
        self._equity += result.net_pnl_usd
        return result

    def _skip(self, trade_date: date, reason: str) -> None:
        log.warning("%s: SKIP (buyer) — %s", trade_date, reason)
        self.skipped.append(SkippedDay(trade_date, reason))


