"""
backtest/run_stoploss_comparison.py
===================================
Compare backtest results: Current Config (no SL) vs Proposed 50% Stop-loss.

It runs:
1. Current Config: With momentum filter (threshold 1.2%, 2h lookback), no stop-loss.
2. Proposed Config: With momentum filter (threshold 1.2%, 2h lookback), 50% cumulative stop-loss.
3. Raw Strategy: No momentum filter, no stop-loss.
4. Raw Strategy + 50% Stop-loss: No momentum filter, 50% cumulative stop-loss.

Calculates key metrics side-by-side:
- Total Trades
- Win Rate (%)
- Net P&L (USD)
- Net Return (%)
- Max Drawdown (USD & %)
- Sharpe Ratio
- Calmar Ratio
- Profit Factor
- SL Hit Count
- Avg Hold Time (minutes)
"""

from __future__ import annotations

import argparse
import logging
import sys
import yaml
from datetime import datetime, time, date, timedelta
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import pandas as pd
import numpy as np

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow running from the opt-algo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, DATA_DIR, REPORTS_DIR
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine
from backtest.portfolio import Portfolio
from backtest.price_engine import find_atm_strike

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_time_utc(time_str: str, tz_name: str) -> time:
    """Parse time string into UTC time object."""
    hour, minute = map(int, time_str.split(":"))
    dt = datetime(2025, 1, 1, hour, minute)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        localized = dt.replace(tzinfo=tz)
        return localized.astimezone(ZoneInfo("UTC")).time()
    except Exception:
        # Fallback for Asia/Kolkata -> UTC (UTC+5:30 -> subtract 5h 30m)
        total_minutes = hour * 60 + minute - 330
        if total_minutes < 0:
            total_minutes += 24 * 60
        return time((total_minutes // 60) % 24, total_minutes % 60)


def load_btc_straddle_settings() -> dict:
    """Load settings from config/settings.yaml for btc_short_straddle."""
    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    defaults = {
        "name": "btc_short_straddle",
        "underlying": "BTC",
        "entry_time": "17:00",
        "exit_time": "17:30",
        "timezone": "Asia/Kolkata",
        "capital_allocation_pct": 50.0,
        "lot_size": None,
        "max_lot_size": 1000,
        "leverage": 200,
        "skip_weekends": False,
        "stop_loss": None,
        "momentum_filter": {
            "enabled": True,
            "lookback_hours": 2.0,
            "threshold_pct": 1.2
        }
    }
    if not settings_path.exists():
        return defaults
    try:
        with open(settings_path, "r") as f:
            data = yaml.safe_load(f) or {}
            straddles = data.get("straddle_strategies", [])
            for strat in straddles:
                if strat.get("name") == "btc_short_straddle":
                    # Merge with defaults, especially nested dicts
                    merged = defaults.copy()
                    for k, v in strat.items():
                        if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
                            merged[k] = merged[k].copy()
                            merged[k].update(v)
                        else:
                            merged[k] = v
                    return merged
    except Exception as e:
        print(f"Error loading settings.yaml: {e}")
    return defaults


def estimate_spot_at_time(
    day_df: pd.DataFrame,
    trade_date: date,
    target_time: time,
    window_minutes: int = 10,
) -> Optional[float]:
    """Estimate BTC spot price at a given time using ATM strike."""
    return find_atm_strike(day_df, trade_date, target_time, window_minutes)


def should_skip_momentum(
    day_df: pd.DataFrame,
    trade_date: date,
    entry_time_utc: time,
    lookback_hours: float,
    threshold_pct: float,
    window_minutes: int = 10,
) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """Evaluate momentum filter skip logic."""
    entry_dt = datetime.combine(trade_date, entry_time_utc)
    lookback_dt = entry_dt - timedelta(hours=lookback_hours)

    if lookback_dt.date() != trade_date:
        return False, None, None, None

    lookback_time = lookback_dt.time()
    spot_lookback = estimate_spot_at_time(day_df, trade_date, lookback_time, window_minutes)
    spot_entry = estimate_spot_at_time(day_df, trade_date, entry_time_utc, window_minutes)

    if spot_lookback is None or spot_entry is None or spot_lookback == 0:
        return False, spot_lookback, spot_entry, None

    move_pct = abs(spot_entry - spot_lookback) / spot_lookback * 100.0
    skip = move_pct > threshold_pct

    return skip, spot_lookback, spot_entry, move_pct


# ---------------------------------------------------------------------------
# Run Variant
# ---------------------------------------------------------------------------

def run_backtest_variant(
    all_days: List[Tuple[date, pd.DataFrame]],
    cfg: BacktestConfig,
    use_momentum: bool,
    momentum_threshold: float,
    momentum_lookback: float,
    skip_weekends: bool,
) -> Portfolio:
    """Run a backtest variant over pre-loaded days."""
    engine = ShortStraddleEngine(cfg)
    for trade_date, day_df in all_days:
        if skip_weekends and trade_date.weekday() in (5, 6):
            engine._skip(trade_date, "weekend trade (Saturday/Sunday)")
            continue
        
        if use_momentum:
            skip, spot_lb, spot_entry, move_pct = should_skip_momentum(
                day_df, trade_date, cfg.entry_time_utc,
                momentum_lookback, momentum_threshold,
            )
            if skip:
                reason = (
                    f"momentum filter: {move_pct:.2f}% move in {momentum_lookback:.0f}h "
                    f"(>{momentum_threshold:.1f}% threshold) | "
                    f"spot {spot_lb:.0f} -> {spot_entry:.0f}"
                )
                engine._skip(trade_date, reason)
                continue

        engine.run_day(trade_date, day_df)
    
    return Portfolio(cfg, engine.trades)


# ---------------------------------------------------------------------------
# Main Comparison Run
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BTC Options Stop-Loss Comparison")
    parser.add_argument("--start", default="2025-01", help="Start month YYYY-MM")
    parser.add_argument("--end", default="2026-06", help="End month YYYY-MM")
    parser.add_argument("--capital", type=float, default=1000.0, help="Initial capital USD")
    parser.add_argument("--verbose", action="store_true", help="Print verbose debug logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    print("=" * 70)
    print("  BTC SHORT STRADDLE STOP-LOSS BACKTEST COMPARISON")
    print("=" * 70)

    # 1. Load configuration from settings.yaml
    settings = load_btc_straddle_settings()
    entry_time_utc = parse_time_utc(settings["entry_time"], settings["timezone"])
    exit_time_utc = parse_time_utc(settings["exit_time"], settings["timezone"])
    
    momentum_conf = settings.get("momentum_filter", {})
    momentum_enabled = momentum_conf.get("enabled", True)
    momentum_threshold = float(momentum_conf.get("threshold_pct", 1.2))
    momentum_lookback = float(momentum_conf.get("lookback_hours", 2.0))

    skip_weekends = settings.get("skip_weekends", False)
    capital_allocation_pct = float(settings.get("capital_allocation_pct", 50.0))
    lot_size = settings.get("lot_size")
    max_lot_size = settings.get("max_lot_size", 1000)
    leverage = float(settings.get("leverage", 200))
    option_margin_pct = float(settings.get("option_margin_requirement_pct", 10.0))

    print(f"Loaded Settings from settings.yaml:")
    print(f"  Underlying   : {settings['underlying']}")
    print(f"  Entry / Exit : {settings['entry_time']} / {settings['exit_time']} {settings['timezone']} "
          f"({entry_time_utc} / {exit_time_utc} UTC)")
    print(f"  Allocation % : {capital_allocation_pct}%")
    print(f"  Lot Size     : {'Dynamic' if lot_size is None else lot_size} (max: {max_lot_size})")
    print(f"  Leverage     : {leverage}x (margin requirement: {option_margin_pct}%)")
    print(f"  Skip Weekends: {skip_weekends}")
    print(f"  Momentum Filt: {'Enabled' if momentum_enabled else 'Disabled'} ({momentum_threshold}% threshold, {momentum_lookback}h lookback)")
    print("-" * 70)

    # 2. Load historical options tick data
    print("Loading historical options data from CSV files ...")
    temp_cfg = BacktestConfig(start_month=args.start, end_month=args.end)
    all_days = []
    for trade_date, day_df in iter_trading_days(temp_cfg):
        all_days.append((trade_date, day_df))
    print(f"Loaded {len(all_days)} trading days in range {args.start} -> {args.end}")
    print("-" * 70)

    if not all_days:
        print("Error: No trading data loaded. Check data path and date range.")
        sys.exit(1)

    # Helper function to generate configs
    def get_cfg(sl_pct: float) -> BacktestConfig:
        return BacktestConfig(
            start_month=args.start,
            end_month=args.end,
            entry_time_utc=entry_time_utc,
            exit_time_utc=exit_time_utc,
            use_dynamic_lot_size=(lot_size is None),
            capital_allocation_pct=capital_allocation_pct,
            leverage=leverage,
            option_margin_requirement_pct=option_margin_pct,
            max_lot_size=max_lot_size if max_lot_size is not None else 1000,
            lot_size=lot_size if lot_size is not None else 150,
            initial_capital=args.capital,
            sl_pct=sl_pct,
            verbose=args.verbose,
        )

    # Run the 4 variants
    # Variant 1: Current Config (Momentum Filter Enabled, No Stop Loss)
    print("Running Variant 1: Current Config (Momentum Filter, No Stop-Loss) ...")
    port_v1 = run_backtest_variant(
        all_days=all_days,
        cfg=get_cfg(sl_pct=9999.0), # No SL
        use_momentum=momentum_enabled,
        momentum_threshold=momentum_threshold,
        momentum_lookback=momentum_lookback,
        skip_weekends=skip_weekends
    )

    # Variant 2: Current Config + 50% Stop Loss
    print("Running Variant 2: Current Config + 50% Stop-Loss ...")
    port_v2 = run_backtest_variant(
        all_days=all_days,
        cfg=get_cfg(sl_pct=50.0), # 50% SL
        use_momentum=momentum_enabled,
        momentum_threshold=momentum_threshold,
        momentum_lookback=momentum_lookback,
        skip_weekends=skip_weekends
    )

    # Variant 3: Raw Strategy (No Momentum Filter, No Stop Loss)
    print("Running Variant 3: Raw Strategy (No Momentum Filter, No Stop-Loss) ...")
    port_v3 = run_backtest_variant(
        all_days=all_days,
        cfg=get_cfg(sl_pct=9999.0), # No SL
        use_momentum=False,
        momentum_threshold=momentum_threshold,
        momentum_lookback=momentum_lookback,
        skip_weekends=skip_weekends
    )

    # Variant 4: Raw Strategy + 50% Stop Loss
    print("Running Variant 4: Raw Strategy + 50% Stop-Loss ...")
    port_v4 = run_backtest_variant(
        all_days=all_days,
        cfg=get_cfg(sl_pct=50.0), # 50% SL
        use_momentum=False,
        momentum_threshold=momentum_threshold,
        momentum_lookback=momentum_lookback,
        skip_weekends=skip_weekends
    )

    print("-" * 70)
    print("Backtests finished. Formatting comparison ...")
    print("-" * 70)

    # 3. Print side-by-side comparison table
    variants = [
        ("V1: Current Config\n(Momentum, No SL)", port_v1),
        ("V2: Current + 50% SL\n(Momentum, 50% SL)", port_v2),
        ("V3: Raw Straddle\n(No Momentum, No SL)", port_v3),
        ("V4: Raw + 50% SL\n(No Momentum, 50% SL)", port_v4),
    ]

    # Metrics
    metrics_to_print = [
        ("Total Trades", lambda p: f"{p.stats['total_trades']}"),
        ("Win Rate (%)", lambda p: f"{p.stats['win_rate_pct']:.1f}%"),
        ("Net P&L (USD)", lambda p: f"${p.stats['total_pnl_usd']:+,.2f}"),
        ("Net Return (%)", lambda p: f"{p.stats['total_return_pct']:+.1f}%"),
        ("Sharpe Ratio", lambda p: f"{p.stats['sharpe_ratio']:.2f}"),
        ("Calmar Ratio", lambda p: f"{p.stats['calmar_ratio']:.2f}"),
        ("Profit Factor", lambda p: f"{p.stats['profit_factor']:.2f}"),
        ("Max Drawdown", lambda p: f"${p.stats['max_drawdown_usd']:+,.2f} ({p.stats['max_drawdown_pct']:.1f}%)"),
        ("SL Hits", lambda p: f"{p.stats['sl_hit_count']}"),
        ("Time Exits", lambda p: f"{p.stats['time_exit_count']}"),
        ("Avg Hold Time", lambda p: f"{p.stats['avg_hold_minutes']:.1f} min"),
        ("Total Fees (USD)", lambda p: f"${p.stats['total_fee_usd']:,.2f}"),
        ("Final Equity", lambda p: f"${p.stats['final_equity']:,.2f}"),
    ]

    header_cols = ["Metric"] + [v[0].replace('\n', ' ') for v in variants]
    print("  " + "  ".join(f"{h:>25s}" for h in header_cols))
    print("-" * 135)
    for label, fn in metrics_to_print:
        vals = [fn(v[1]) for v in variants]
        row_str = f"  {label:<25s}" + "  ".join(f"{val:>25s}" for val in vals)
        print(row_str)
    print("=" * 135)

    # 4. Generate Plot (Matplotlib)
    try:
        import matplotlib.pyplot as plt
        plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')
        fig, ax = plt.subplots(figsize=(12, 7))

        for label, port in variants:
            df = port.trade_df
            if not df.empty:
                ax.plot(df['date'], df['equity'], label=label.replace('\n', ' '), linewidth=2)

        ax.set_title("BTC Options Short Straddle Equity Curve Comparison (Jan 2025 - Jun 2026)", fontsize=14, fontweight='bold')
        ax.set_xlabel("Date", fontsize=12)
        ax.set_ylabel("Account Value (USD)", fontsize=12)
        ax.legend(fontsize=10, loc='upper left')
        ax.tick_params(labelsize=10)
        
        # Save plot to artifacts dir
        artifacts_dir = Path(r"C:\Users\gopin\.gemini\antigravity-ide\brain\eea92b33-03a6-474d-adc9-70a5a762b2bf")
        plot_path = artifacts_dir / "equity_curves.png"
        plt.tight_layout()
        plt.savefig(plot_path, dpi=150)
        print(f"\nSaved equity curves comparison plot to {plot_path}")
        plt.close()
    except Exception as e:
        print(f"\nFailed to generate matplotlib plot: {e}")

    # 5. Generate Markdown Report
    try:
        report_path = Path(r"C:\Users\gopin\.gemini\antigravity-ide\brain\eea92b33-03a6-474d-adc9-70a5a762b2bf") / "stoploss_comparison_report.md"
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write("# BTC Options Stop-Loss Comparison Backtest Report\n\n")
            rf.write(f"**Date Range**: {args.start} to {args.end}  \n")
            rf.write(f"**Initial Capital**: ${args.capital:,.2f}  \n")
            rf.write(f"**Execution Timestamp**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n\n")

            rf.write("## Overview & Objectives\n\n")
            rf.write("This report evaluates the performance impact of adding a **50% cumulative premium stop-loss** ")
            rf.write("to the daily BTC short straddle options strategy. We compare it side-by-side with the current ")
            rf.write("live configuration (which utilizes a pre-entry momentum filter but no active stop-loss).\n\n")

            rf.write("## Strategy Variants Evaluated\n\n")
            rf.write("1. **V1: Current Config**: Momentum filter enabled (1.2% threshold / 2h lookback), no stop-loss (held to exit).\n")
            rf.write("2. **V2: Current + 50% SL**: Momentum filter enabled, with a 50% cumulative premium stop-loss.\n")
            rf.write("3. **V3: Raw Straddle**: No momentum filter, no stop-loss (held to exit).\n")
            rf.write("4. **V4: Raw + 50% SL**: No momentum filter, with a 50% cumulative premium stop-loss.\n\n")

            rf.write("## Comparative Metrics Table\n\n")
            rf.write("| Metric | V1: Current Config | V2: Current + 50% SL | V3: Raw Straddle | V4: Raw + 50% SL |\n")
            rf.write("|---|---|---|---|---|\n")
            for label, fn in metrics_to_print:
                rf.write(f"| {label} | {fn(port_v1)} | {fn(port_v2)} | {fn(port_v3)} | {fn(port_v4)} |\n")
            rf.write("\n")

            rf.write("## Key Findings & Quantitative Analysis\n\n")
            
            # Write custom qualitative summary based on results
            pnl_diff = port_v2.stats['total_pnl_usd'] - port_v1.stats['total_pnl_usd']
            dd_diff = port_v2.stats['max_drawdown_pct'] - port_v1.stats['max_drawdown_pct']
            sharpe_diff = port_v2.stats['sharpe_ratio'] - port_v1.stats['sharpe_ratio']
            sl_hits = port_v2.stats['sl_hit_count']
            
            rf.write("### 1. Stop-Loss Performance Impact\n")
            rf.write(f"- **P&L Shift**: Adding the stop-loss changed Net P&L by **${pnl_diff:+,.2f}** compared to the baseline config.\n")
            rf.write(f"- **Risk Reduction**: Max Drawdown shifted by **{dd_diff:+.1f}%** (from {port_v1.stats['max_drawdown_pct']:.1f}% to {port_v2.stats['max_drawdown_pct']:.1f}%).\n")
            rf.write(f"- **Risk-Adjusted Return**: The Sharpe Ratio changed by **{sharpe_diff:+.2f}** (from {port_v1.stats['sharpe_ratio']:.2f} to {port_v2.stats['sharpe_ratio']:.2f}).\n")
            rf.write(f"- **Stop-Loss Triggers**: The stop-loss was triggered **{sl_hits} times** out of {port_v2.stats['total_trades']} total trades.\n\n")

            rf.write("### 2. Interaction with the Pre-Entry Momentum Filter\n")
            rf.write("- Comparing V1 (Current Config) vs V3 (Raw Straddle) demonstrates the effect of the Pre-Entry Momentum Filter alone.\n")
            rf.write("- Comparing V2 (Current + 50% SL) vs V4 (Raw + 50% SL) demonstrates the combined protection of pre-entry filters and active intraday risk management.\n")
            rf.write("\n")
            
            rf.write("### 3. Conclusion & Recommendations\n")
            if port_v2.stats['total_pnl_usd'] > port_v1.stats['total_pnl_usd']:
                rf.write("> [!TIP]\n")
                rf.write("> **Recommendation**: **Adopt the 50% stop-loss.** The backtest shows that adding a stop-loss improves net profitability and reduces drawdown. This indicates that cut-short losses outweigh premium restoration/recovery on high-volatility days.\n")
            else:
                rf.write("> [!NOTE]\n")
                rf.write("> **Analysis**: The stop-loss reduced net return, which is common in short straddles as option premiums often expand intraday before decaying back by the settlement time. However, the stop-loss provides a crucial safety net against extreme tail risk (black swan events) by capping the maximum daily loss per trade.\n")

        print(f"Generated Markdown report at {report_path}\n")
    except Exception as e:
        print(f"Failed to generate markdown report: {e}")


if __name__ == "__main__":
    main()
