"""
backtest/data_loader.py
=======================
Loads and parses monthly BTC options CSV files from Deribit.

Symbol format: {C|P}-BTC-{STRIKE}-{DDMMYY}
  e.g.  C-BTC-96800-030125 → Call, strike=96800, expiry=2025-01-03

Each CSV row: product_symbol, price, size, timestamp, buyer_role
Timestamps are UTC.

The loader yields one pandas DataFrame per trading day, containing
ONLY same-day expiry options (as required by the strategy).
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from pathlib import Path
from typing import Generator, List

import pandas as pd

from .config import BacktestConfig, DATA_DIR

log = logging.getLogger(__name__)


def _months_in_range(start_month: str, end_month: str) -> List[str]:
    """Return list of 'YYYY-MM' strings between start and end inclusive."""
    start = datetime.strptime(start_month, "%Y-%m")
    end   = datetime.strptime(end_month,   "%Y-%m")
    months = []
    cur = start
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


def load_month_raw(month: str, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """
    Load a full month CSV, parse symbols, and add derived columns:
      opt_type  : 'C' or 'P'
      strike    : float
      expiry    : date
      ts        : datetime (UTC, timezone-naive)
    """
    path = data_dir / f"BTC_{month}.csv"
    if not path.exists():
        log.warning("File not found: %s — skipping month %s", path, month)
        return pd.DataFrame()

    log.info("Loading %s (%.0f MB) …", path.name, path.stat().st_size / 1e6)

    df = pd.read_csv(
        path,
        dtype={
            "product_symbol": "string",
            "price":          "float32",
            "size":           "float32",
            "buyer_role":     "string",
        },
    )

    # Rename timestamp column
    df.rename(columns={"timestamp": "ts"}, inplace=True)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"])

    # ---- Parse symbol into components ------------------------------------
    parsed = df["product_symbol"].str.extract(
        r'^(?P<opt_type>[CP])-BTC-(?P<strike_str>\d+)-(?P<expiry_str>\d{6})$'
    )
    df["opt_type"]   = parsed["opt_type"]
    df["strike"]     = pd.to_numeric(parsed["strike_str"], errors="coerce")
    df["expiry_str"] = parsed["expiry_str"]

    # Drop rows that didn't match the expected symbol format
    bad = df["strike"].isna()
    if bad.any():
        log.debug("Dropped %d rows with unexpected symbol format", bad.sum())
        df = df[~bad].copy()

    # Parse expiry date: DDMMYY → date
    df["expiry"] = pd.to_datetime(
        df["expiry_str"], format="%d%m%y", errors="coerce"
    ).dt.date

    df = df.dropna(subset=["expiry"])

    # Add trade_date column (UTC date of the tick)
    df["trade_date"] = df["ts"].dt.date

    log.info("  → %d rows, dates %s to %s",
             len(df), df["trade_date"].min(), df["trade_date"].max())
    return df


def iter_trading_days(
    cfg: BacktestConfig,
) -> Generator[tuple[date, pd.DataFrame], None, None]:
    """
    Yield (trade_date, day_df) for every trading day in the configured range.

    day_df contains only same-day expiry options, sorted by ts ascending.
    """
    months = _months_in_range(cfg.start_month, cfg.end_month)

    for month in months:
        month_df = load_month_raw(month, DATA_DIR)
        if month_df.empty:
            continue

        for trade_date, group in month_df.groupby("trade_date"):
            # Same-day expiry filter
            if cfg.expiry_filter == "same_day":
                day_df = group[group["expiry"] == trade_date].copy()
            else:
                day_df = group.copy()

            if day_df.empty:
                log.debug("No same-day expiry data for %s — skip", trade_date)
                continue

            day_df.sort_values("ts", inplace=True)
            day_df.reset_index(drop=True, inplace=True)

            yield trade_date, day_df
