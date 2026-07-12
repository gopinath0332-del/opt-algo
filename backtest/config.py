"""
backtest/config.py
==================
All configuration constants for the short-straddle backtest.
Mirrors strategy parameters from config/settings.yaml and adds
backtest-specific settings.
"""

from dataclasses import dataclass, field
from pathlib import Path
from datetime import time


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = Path(r"D:\Workspace\crypto-backtest-data\options")
REPORTS_DIR = Path(__file__).parent / "reports"


# ---------------------------------------------------------------------------
# BacktestConfig
# ---------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    # ---- Data range -------------------------------------------------------
    start_month: str = "2025-01"        # inclusive, format YYYY-MM
    end_month: str   = "2026-06"        # inclusive, format YYYY-MM

    # ---- Strategy timing (UTC) -------------------------------------------
    # 17:00 IST = 11:30 UTC  |  17:25 IST = 11:55 UTC
    entry_time_utc: time = field(default_factory=lambda: time(11, 30))
    exit_time_utc:  time = field(default_factory=lambda: time(11, 55))

    # Price lookup: search within ±N minutes of target time
    price_window_minutes: int = 5

    # ---- Position sizing --------------------------------------------------
    lot_size: int = 150          # contracts per leg (call + put)
    initial_capital: float = 1_000.0   # USD — for % return calculation

    # ---- Stop-loss --------------------------------------------------------
    # Exit when combined premium loss >= sl_pct % of entry premium
    sl_pct: float = 50.0         # 50 %

    # ---- Expiry filter ----------------------------------------------------
    # "same_day" → only options expiring on the trade date
    expiry_filter: str = "same_day"

    # ---- SL monitoring ----------------------------------------------------
    # "tick" → evaluate SL on every tick between entry and exit
    sl_mode: str = "tick"

    # ---- Strike selection -------------------------------------------------
    # "parity" → pick strike where |call_price - put_price| is minimised
    strike_selection: str = "parity"

    # ---- Reporting --------------------------------------------------------
    report_dir: Path = field(default_factory=lambda: REPORTS_DIR)
    verbose: bool = True


# Singleton default config used by the engine unless overridden
DEFAULT_CONFIG = BacktestConfig()
