"""
backtest/config.py
==================
All configuration constants for the short-straddle backtest.
Dynamically loads and mirrors live strategy parameters from config/settings.yaml
to keep backtest and live execution completely in sync.
"""

from dataclasses import dataclass, field
from pathlib import Path
from datetime import time, datetime, timedelta
from zoneinfo import ZoneInfo
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = Path(r"D:\Workspace\crypto-backtest-data\options")
REPORTS_DIR = Path(__file__).parent / "reports"

# ---------------------------------------------------------------------------
# Dynamic settings loading from live config/settings.yaml
# ---------------------------------------------------------------------------
def _load_live_settings() -> dict:
    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    if not settings_path.exists():
        return {}
    try:
        with open(settings_path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def _parse_time_utc(time_str: str, tz_name: str) -> time:
    hour, minute = map(int, time_str.split(":"))
    dt = datetime(2025, 1, 1, hour, minute)
    try:
        tz = ZoneInfo(tz_name)
        localized = dt.replace(tzinfo=tz)
        return localized.astimezone(ZoneInfo("UTC")).time()
    except Exception:
        # Fallback for Asia/Kolkata -> UTC (UTC+5:30 -> subtract 5h 30m)
        total_minutes = hour * 60 + minute - 330
        if total_minutes < 0:
            total_minutes += 24 * 60
        return time((total_minutes // 60) % 24, total_minutes % 60)

_settings = _load_live_settings()
_strat = _settings.get("strategy", {})

LIVE_ENTRY_TIME         = _parse_time_utc(_strat.get("entry_time", "17:00"), _strat.get("timezone", "Asia/Kolkata"))
LIVE_EXIT_TIME          = _parse_time_utc(_strat.get("exit_time",  "17:25"), _strat.get("timezone", "Asia/Kolkata"))
LIVE_LOT_SIZE           = _strat.get("lot_size", None)          # None = dynamic in live bot
LIVE_CAPITAL_ALLOC_PCT  = float(_strat.get("capital_allocation_pct", 60))
LIVE_LEVERAGE           = float(_strat.get("leverage", 200))
LIVE_OPTION_MARGIN_PCT  = float(_strat.get("option_margin_requirement_pct", 10.0))
_sl_conf = _strat.get("stop_loss")
if isinstance(_sl_conf, dict):
    LIVE_SL_PCT = float(_sl_conf.get("value", 9999.0))
else:
    LIVE_SL_PCT = 9999.0

# ---------------------------------------------------------------------------
# BacktestConfig
# ---------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    # ---- Data range -------------------------------------------------------
    start_month: str = "2025-01"        # inclusive, format YYYY-MM
    end_month: str   = "2026-06"        # inclusive, format YYYY-MM

    # ---- Strategy timing (UTC) -------------------------------------------
    # Mirrored dynamically from config/settings.yaml (e.g. 17:00 IST -> 11:30 UTC)
    entry_time_utc: time = field(default_factory=lambda: LIVE_ENTRY_TIME)
    exit_time_utc:  time = field(default_factory=lambda: LIVE_EXIT_TIME)

    # Price lookup: search within ±N minutes of target time
    price_window_minutes: int = 5

    # ---- Position sizing --------------------------------------------------
    # When use_dynamic_lot_size=True (default), lot_size is computed per-day
    # using the same margin-based formula as the live bot:
    #
    #   lot_size = floor(equity × capital_allocation_pct% / total_margin_per_lot)
    #   total_margin_per_lot = 2 × spot × contract_value × option_margin_requirement_pct / 100
    #
    # max_lot_size caps the computed value to prevent runaway compounding
    # (exchange liquidity / account limits make unlimited scaling unrealistic).
    # Set to 0 to disable the cap.
    #
    # lot_size below is only used when use_dynamic_lot_size=False.
    use_dynamic_lot_size:    bool  = True
    capital_allocation_pct:  float = field(default_factory=lambda: LIVE_CAPITAL_ALLOC_PCT)
    leverage:                float = field(default_factory=lambda: LIVE_LEVERAGE)
    option_margin_requirement_pct: float = field(default_factory=lambda: LIVE_OPTION_MARGIN_PCT)
    max_lot_size:            int   = 10_000        # hard cap per leg (0 = no cap)
    lot_size:                int   = field(default_factory=lambda: int(LIVE_LOT_SIZE) if LIVE_LOT_SIZE else 150)
    contract_value:          float = 0.001         # BTC contract size multiplier
    initial_capital:         float = 1_000.0       # USD — starting equity

    # ---- Stop-loss --------------------------------------------------------
    sl_pct: float = LIVE_SL_PCT          # Dynamically mirrored from config/settings.yaml

    # Deribit/Delta options fee rate (0.03% of underlying notional per side)
    fee_rate: float = 0.0003     # 0.03% of underlying
    fee_cap_pct: float = 3.5     # Capped at 3.5% of option premium per leg

    # ---- Slippage ---------------------------------------------------------
    # % of option premium lost to slippage on each leg, each side.
    # Entry: receive premium × (1 - slippage_pct/100)
    # Exit:  pay    premium × (1 + slippage_pct/100)
    slippage_pct: float = 0.0    # 0 % default (no slippage)

    # ---- Expiry filter ----------------------------------------------------
    # "same_day" → only options expiring on the trade date
    expiry_filter: str = "same_day"

    # ---- SL monitoring ----------------------------------------------------
    # "tick"   → evaluate SL on every raw trade tick
    # "minute" → evaluate SL on 1-minute close (filters out execution noise)
    sl_mode: str = "minute"

    # ---- Strike selection -------------------------------------------------
    # "parity" → pick strike where |call_price - put_price| is minimised
    strike_selection: str = "parity"

    # OTM strangle: number of $200 strike steps away from ATM for each leg.
    # 0  = ATM straddle (default, backward compatible)
    # 10 = 10th OTM strike ($2,000 away from spot for both call and put)
    otm_steps: int = 0

    # ---- Reporting --------------------------------------------------------
    report_dir: Path = field(default_factory=lambda: REPORTS_DIR)
    verbose: bool = True


# Singleton default config used by the engine unless overridden
DEFAULT_CONFIG = BacktestConfig()
