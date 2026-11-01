"""
backtest/candle_utils.py
========================
Utilities for candle transformations: Standard OHLCV to Heikin Ashi.
"""

from __future__ import annotations

from typing import Optional
import numpy as np
import pandas as pd


def convert_to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converts a standard OHLCV DataFrame to Heikin Ashi candles.
    Expected columns: 'open', 'high', 'low', 'close'

    Formulas:
        HA_Close = (Open + High + Low + Close) / 4
        HA_Open[0] = Open[0]
        HA_Open[i] = (HA_Open[i-1] + HA_Close[i-1]) / 2
        HA_High = max(High, HA_Open, HA_Close)
        HA_Low  = min(Low,  HA_Open, HA_Close)
    """
    ha_df = df.copy()

    # HA Close
    ha_df["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0

    # HA Open
    n = len(df)
    ha_open = np.zeros(n)
    if n > 0:
        ha_open[0] = df["open"].iloc[0]
        for i in range(1, n):
            ha_open[i] = (ha_open[i - 1] + ha_df["close"].iloc[i - 1]) / 2.0
    ha_df["open"] = ha_open

    # HA High & Low
    ha_df["high"] = np.maximum(df["high"], np.maximum(ha_df["open"], ha_df["close"]))
    ha_df["low"] = np.minimum(df["low"], np.minimum(ha_df["open"], ha_df["close"]))

    return ha_df

