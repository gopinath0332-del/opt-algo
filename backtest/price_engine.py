"""
backtest/price_engine.py
========================
Handles all price-related queries against a day's tick DataFrame.

Core responsibilities:
  1. get_price_at_time()      — VWAP for a specific option near a target time
  2. find_atm_strike()        — infer ATM strike from put-call parity
  3. get_straddle_price()     — combined call+put prices at a given time
  4. get_tick_prices_after_entry() — streaming tick prices for SL monitoring
"""

from __future__ import annotations

import logging
from datetime import date, time, datetime
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _center_ts(trade_date: date, t: time) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(trade_date, t))


def _window_mask(
    ts_series: pd.Series,
    center: pd.Timestamp,
    window_minutes: int,
) -> pd.Series:
    delta = pd.Timedelta(minutes=window_minutes)
    return (ts_series >= center - delta) & (ts_series <= center + delta)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_price_at_time(
    day_df: pd.DataFrame,
    opt_type: str,
    strike: float,
    trade_date: date,
    target_time: time,
    window_minutes: int = 5,
) -> Optional[float]:
    """
    Return VWAP for (opt_type, strike) within the time window.
    Falls back to most-recent trade before center+window if window is empty.
    """
    center = _center_ts(trade_date, target_time)
    mask = (
        (day_df["opt_type"] == opt_type) &
        (day_df["strike"]   == strike)   &
        _window_mask(day_df["ts"], center, window_minutes)
    )
    subset = day_df[mask]

    if subset.empty:
        cutoff = center + pd.Timedelta(minutes=window_minutes)
        prior = day_df[
            (day_df["opt_type"] == opt_type) &
            (day_df["strike"]   == strike)   &
            (day_df["ts"]       <= cutoff)
        ]
        if prior.empty:
            return None
        return float(prior.iloc[-1]["price"])

    # Volume-weighted average price
    vwap = float((subset["price"] * subset["size"]).sum() / subset["size"].sum())
    return vwap


def find_atm_strike(
    day_df: pd.DataFrame,
    trade_date: date,
    target_time: time,
    window_minutes: int = 5,
) -> Optional[float]:
    """
    Find ATM strike by minimising |call_vwap - put_vwap| at entry time.
    Uses put-call parity: when C_price ≈ P_price, strike ≈ spot.
    Returns None if insufficient data.
    """
    center  = _center_ts(trade_date, target_time)
    mask    = _window_mask(day_df["ts"], center, window_minutes)
    near_df = day_df[mask]

    if near_df.empty:
        log.debug("%s: no trades in entry window", trade_date)
        return None

    # VWAP per (strike, opt_type)
    vwap = (
        near_df
        .groupby(["strike", "opt_type"])
        .apply(
            lambda g: (g["price"] * g["size"]).sum() / g["size"].sum(),
            include_groups=False,
        )
        .rename("vwap")
        .reset_index()
    )

    calls = vwap[vwap["opt_type"] == "C"].set_index("strike")["vwap"]
    puts  = vwap[vwap["opt_type"] == "P"].set_index("strike")["vwap"]

    common = calls.index.intersection(puts.index)
    if common.empty:
        log.debug("%s: no strikes with both C and P in entry window", trade_date)
        return None

    diff = (calls[common] - puts[common]).abs()
    atm  = float(diff.idxmin())
    log.debug(
        "%s: ATM=%g  C=%.2f  P=%.2f  diff=%.2f",
        trade_date, atm,
        float(calls[atm]), float(puts[atm]), float(diff.min()),
    )
    return atm


def get_straddle_price(
    day_df: pd.DataFrame,
    strike: float,
    trade_date: date,
    target_time: time,
    window_minutes: int = 5,
) -> tuple[Optional[float], Optional[float]]:
    """Return (call_price, put_price) VWAP for the given strike at target_time."""
    c = get_price_at_time(day_df, "C", strike, trade_date, target_time, window_minutes)
    p = get_price_at_time(day_df, "P", strike, trade_date, target_time, window_minutes)
    return c, p


STRIKE_STEP = 200.0  # BTC options on Delta Exchange use $200 strike increments


def find_otm_strikes(
    day_df: pd.DataFrame,
    atm_strike: float,
    otm_steps: int,
    strike_step: float = STRIKE_STEP,
) -> tuple[Optional[float], Optional[float]]:
    """
    Return (call_strike, put_strike) that are `otm_steps` increments away from ATM.

    call_strike = atm_strike + otm_steps * strike_step
    put_strike  = atm_strike - otm_steps * strike_step

    Returns None for a leg if that strike has no data in day_df.
    """
    call_strike = atm_strike + otm_steps * strike_step
    put_strike  = atm_strike - otm_steps * strike_step

    # Verify the strikes actually exist in today's data
    has_call = ((day_df["opt_type"] == "C") & (day_df["strike"] == call_strike)).any()
    has_put  = ((day_df["opt_type"] == "P") & (day_df["strike"] == put_strike)).any()

    return (call_strike if has_call else None,
            put_strike  if has_put  else None)


def get_strangle_price(
    day_df: pd.DataFrame,
    call_strike: float,
    put_strike: float,
    trade_date: date,
    target_time: time,
    window_minutes: int = 5,
) -> tuple[Optional[float], Optional[float]]:
    """Return (call_price, put_price) VWAP for asymmetric call/put strikes."""
    c = get_price_at_time(day_df, "C", call_strike, trade_date, target_time, window_minutes)
    p = get_price_at_time(day_df, "P", put_strike,  trade_date, target_time, window_minutes)
    return c, p


def get_tick_prices_after_entry(
    day_df: pd.DataFrame,
    call_strike: float,
    put_strike: float,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    entry_call: float,
    entry_put: float,
) -> pd.DataFrame:
    """
    Return all ticks for the straddle legs between entry_ts and exit_ts.
    Uses entry_call and entry_put to initialize the prices at entry_ts,
    then forward-fills subsequent trade ticks to prevent stale pre-entry price leakage.
    """
    # Create the starting entry row
    start_df = pd.DataFrame([{
        "ts": entry_ts,
        "call_price": entry_call,
        "put_price": entry_put
    }]).set_index("ts")

    mask = (
        (
            ((day_df["opt_type"] == "C") & (day_df["strike"] == call_strike)) |
            ((day_df["opt_type"] == "P") & (day_df["strike"] == put_strike))
        ) &
        (day_df["ts"] > entry_ts) &
        (day_df["ts"] <= exit_ts)
    )
    ticks = day_df[mask].copy()

    if ticks.empty:
        start_df.reset_index(inplace=True)
        return start_df

    ticks = ticks.sort_values("ts")

    c_ticks = (
        ticks[ticks["opt_type"] == "C"][["ts", "price"]]
        .rename(columns={"price": "call_price"})
        .set_index("ts")
    )
    p_ticks = (
        ticks[ticks["opt_type"] == "P"][["ts", "price"]]
        .rename(columns={"price": "put_price"})
        .set_index("ts")
    )

    combined = pd.concat([c_ticks, p_ticks], axis=1).sort_index()
    
    # Prepend starting entry row so we start tracking from exactly entry prices
    combined = pd.concat([start_df, combined]).sort_index()
    combined = combined.ffill()
    
    combined.reset_index(inplace=True)
    return combined
