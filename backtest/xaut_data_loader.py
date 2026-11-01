"""
backtest/xaut_data_loader.py
============================
Fetches and caches XAUTUSD 1H OHLCV candles from Delta Exchange.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from api.rest_client import DeltaRestClient
from core.config import Config

log = logging.getLogger(__name__)

DEFAULT_XAUT_CACHE_PATH = Path(
    r"D:\Workspace\crypto-backtest-data\Futures\delta_crypto_data_1H\XAUTUSD_1h.csv"
)


class XAUTDataLoader:
    """Fetches and manages XAUTUSD historical candle data from Delta Exchange."""

    def __init__(self, client: Optional[DeltaRestClient] = None, symbol: str = "XAUTUSD"):
        if client is None:
            config = Config()
            client = DeltaRestClient(config)
        self.client = client
        self.symbol = symbol

    def fetch_hourly_data(
        self,
        start_ts: int,
        end_ts: int,
        chunk_hours: int = 500,
    ) -> pd.DataFrame:
        """
        Fetch 1H candles for XAUTUSD paginating forward in time.
        Delta Exchange returns candles newest-first.
        """
        all_candles = []
        current_start = start_ts
        chunk_seconds = chunk_hours * 3600

        log.info(
            "Fetching %s 1H candles from %s to %s...",
            self.symbol,
            datetime.fromtimestamp(start_ts, tz=timezone.utc),
            datetime.fromtimestamp(end_ts, tz=timezone.utc),
        )

        while current_start < end_ts:
            current_end = min(current_start + chunk_seconds, end_ts)
            log.info(
                "  Querying chunk: %s to %s",
                datetime.fromtimestamp(current_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                datetime.fromtimestamp(current_end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            )
            candles = self.client.get_candles(
                symbol=self.symbol,
                resolution=60,
                start=current_start,
                end=current_end,
            )

            if candles:
                all_candles.extend(candles)
                log.info("    Received %d candles", len(candles))
            else:
                log.info("    No candles returned for this chunk")

            current_start = current_end
            time.sleep(0.1)  # small pause to be polite

        if not all_candles:
            log.warning("No candle data fetched for %s", self.symbol)
            return pd.DataFrame()

        df = pd.DataFrame(all_candles)
        # Drop duplicates by time
        df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

        # Standardize columns
        df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        log.info("Total %s candles fetched: %d (%s to %s)",
                 self.symbol, len(df), df["dt"].min(), df["dt"].max())
        return df

    def get_or_fetch_data(
        self,
        filepath: Path = DEFAULT_XAUT_CACHE_PATH,
        force_fetch: bool = True,
        start_date: str = "2026-04-17",
    ) -> pd.DataFrame:
        """
        Load cached data or fetch directly from Delta Exchange.
        If force_fetch=True, queries Delta Exchange and saves to CSV.
        """
        if not force_fetch and filepath.exists():
            log.info("Loading cached XAUT data from %s", filepath)
            df = pd.read_csv(filepath)
            if "time" in df.columns:
                df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
            return df

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ts = int(start_dt.timestamp())
        end_ts = int(time.time())

        df = self.fetch_hourly_data(start_ts, end_ts)
        if not df.empty:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            # Format nicely for saving
            export_df = df.copy()
            # Add human readable date and time columns for compatibility
            ist_dt = export_df["dt"].dt.tz_convert("Asia/Kolkata")
            export_df["date"] = ist_dt.dt.strftime("%d/%m/%Y")
            export_df["time_ist"] = ist_dt.dt.strftime("%H:%M:%S")
            export_df["symbol"] = self.symbol
            export_df["resolution"] = "1h"

            cols = ["date", "time_ist", "symbol", "resolution", "open", "high", "low", "close", "volume", "time"]
            available_cols = [c for c in cols if c in export_df.columns]
            export_df[available_cols].to_csv(filepath, index=False)
            log.info("Saved %d candles to %s", len(export_df), filepath)

        return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    loader = XAUTDataLoader()
    df = loader.get_or_fetch_data(force_fetch=True)
    print(df.head())
    print(df.tail())
