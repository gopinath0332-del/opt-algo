"""
backtest/run_momentum_filter_comparison.py
==========================================
Compare the current Short Straddle backtest (baseline) against a version
that adds a **pre-entry momentum filter**.

Momentum filter logic:
  - 2 hours before entry (09:30 UTC, i.e. 15:00 IST), estimate BTC spot
    using the ATM strike (put-call parity).
  - At entry time (11:30 UTC, i.e. 17:00 IST), again estimate spot from ATM.
  - If |spot_entry - spot_2h_ago| / spot_2h_ago > threshold (default 1%),
    skip the trade for that day.

Since the options data does not include spot/futures ticks, we estimate spot
as the ATM strike at a given time using the same put-call parity logic as
the existing backtest engine (find_atm_strike).

Usage:
  python backtest/run_momentum_filter_comparison.py
  python backtest/run_momentum_filter_comparison.py --threshold 1.0
  python backtest/run_momentum_filter_comparison.py --start 2025-06 --end 2026-06
  python backtest/run_momentum_filter_comparison.py --threshold 0.5 1.0 1.5 2.0
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow running from the opt-algo root: python backtest/run_momentum_filter_comparison.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import (
    BacktestConfig, REPORTS_DIR, LIVE_LOT_SIZE, LIVE_SL_PCT, LIVE_CAPITAL_ALLOC_PCT,
)
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, SkippedDay
from backtest.portfolio import Portfolio
from backtest.price_engine import find_atm_strike

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Momentum filter helpers
# ---------------------------------------------------------------------------

def estimate_spot_at_time(
    day_df,
    trade_date: date,
    target_time: time,
    window_minutes: int = 10,
) -> Optional[float]:
    """Estimate BTC spot price at a given time using ATM strike (put-call parity).

    Uses a wider window (default 10 min) to increase the chance of finding
    both call and put ticks at the lookback time.
    """
    return find_atm_strike(day_df, trade_date, target_time, window_minutes)


def should_skip_momentum(
    day_df,
    trade_date: date,
    entry_time_utc: time,
    lookback_hours: float,
    threshold_pct: float,
    window_minutes: int = 10,
) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """Check if the momentum filter should skip this day.

    Returns:
        (skip: bool, spot_lookback, spot_entry, move_pct)
    """
    # Compute lookback time (entry - lookback_hours)
    entry_dt = datetime.combine(trade_date, entry_time_utc)
    lookback_dt = entry_dt - timedelta(hours=lookback_hours)

    # Handle day boundary (lookback might be previous day — we'll skip if so)
    if lookback_dt.date() != trade_date:
        # Can't check previous day's data within this day's DataFrame.
        # Conservative: don't skip (we don't have the data to filter).
        return False, None, None, None

    lookback_time = lookback_dt.time()

    # Estimate spot at lookback and entry times
    spot_lookback = estimate_spot_at_time(day_df, trade_date, lookback_time, window_minutes)
    spot_entry = estimate_spot_at_time(day_df, trade_date, entry_time_utc, window_minutes)

    if spot_lookback is None or spot_entry is None or spot_lookback == 0:
        # Can't compute momentum — don't skip
        return False, spot_lookback, spot_entry, None

    move_pct = abs(spot_entry - spot_lookback) / spot_lookback * 100.0
    skip = move_pct > threshold_pct

    return skip, spot_lookback, spot_entry, move_pct


# ---------------------------------------------------------------------------
# Run both scenarios
# ---------------------------------------------------------------------------

def run_comparison(
    cfg: BacktestConfig,
    thresholds: List[float],
    lookback_hours: float = 2.0,
    skip_weekends: bool = False,
) -> dict:
    """Run baseline + filtered backtests and return results.

    Returns dict:
        {
            "baseline": Portfolio,
            "filtered": {threshold: Portfolio, ...},
            "filter_stats": {threshold: {...}, ...},
        }
    """
    # Pre-collect all trading days to avoid reloading CSVs for each run
    log.info("Loading trading data...")
    all_days: List[Tuple[date, object]] = []
    for trade_date, day_df in iter_trading_days(cfg):
        all_days.append((trade_date, day_df))
    log.info(f"Loaded {len(all_days)} trading days")

    # --- BASELINE (no filter) ---
    log.info("=" * 60)
    log.info("  Running BASELINE (no momentum filter)")
    log.info("=" * 60)
    baseline_engine = ShortStraddleEngine(cfg)
    for trade_date, day_df in all_days:
        if skip_weekends and trade_date.weekday() in (5, 6):
            baseline_engine._skip(trade_date, "weekend trade (Saturday/Sunday)")
            continue
        baseline_engine.run_day(trade_date, day_df)

    baseline_portfolio = Portfolio(cfg, baseline_engine.trades)

    # --- FILTERED (with momentum filter at each threshold) ---
    results = {
        "baseline": baseline_portfolio,
        "filtered": {},
        "filter_stats": {},
    }

    for threshold in thresholds:
        log.info("=" * 60)
        log.info(f"  Running with MOMENTUM FILTER (>{threshold:.1f}% in {lookback_hours:.0f}h)")
        log.info("=" * 60)

        filtered_cfg = BacktestConfig(
            start_month=cfg.start_month,
            end_month=cfg.end_month,
            lot_size=cfg.lot_size,
            sl_pct=cfg.sl_pct,
            initial_capital=cfg.initial_capital,
            capital_allocation_pct=cfg.capital_allocation_pct,
            verbose=cfg.verbose,
        )
        filtered_engine = ShortStraddleEngine(filtered_cfg)
        days_skipped_by_filter = 0
        filter_details = []

        for trade_date, day_df in all_days:
            if skip_weekends and trade_date.weekday() in (5, 6):
                filtered_engine._skip(trade_date, "weekend trade (Saturday/Sunday)")
                continue

            skip, spot_lb, spot_entry, move_pct = should_skip_momentum(
                day_df, trade_date, cfg.entry_time_utc,
                lookback_hours, threshold,
            )

            if skip:
                days_skipped_by_filter += 1
                reason = (
                    f"momentum filter: {move_pct:.2f}% move in {lookback_hours:.0f}h "
                    f"(>{threshold:.1f}% threshold) | "
                    f"spot {spot_lb:.0f} -> {spot_entry:.0f}"
                )
                filtered_engine._skip(trade_date, reason)
                filter_details.append({
                    "date": trade_date,
                    "spot_lookback": spot_lb,
                    "spot_entry": spot_entry,
                    "move_pct": move_pct,
                })
                log.info(f"  {trade_date} | SKIP | {move_pct:.2f}% move")
                continue

            filtered_engine.run_day(trade_date, day_df)

        filtered_portfolio = Portfolio(filtered_cfg, filtered_engine.trades)
        results["filtered"][threshold] = filtered_portfolio
        results["filter_stats"][threshold] = {
            "days_skipped": days_skipped_by_filter,
            "total_days": len(all_days),
            "details": filter_details,
        }

    return results


# ---------------------------------------------------------------------------
# Pretty print comparison
# ---------------------------------------------------------------------------

def print_comparison(results: dict, thresholds: List[float]) -> None:
    """Print a side-by-side comparison table."""
    baseline = results["baseline"]
    b = baseline.stats

    sep = "=" * 90
    thin = "-" * 90

    print(f"\n{sep}")
    print("  MOMENTUM FILTER COMPARISON — BTC Short Straddle Backtest")
    print(f"{sep}")
    print(f"  Period: {baseline.trade_df['date'].min().date()} -> {baseline.trade_df['date'].max().date()}")
    print(f"  Capital: ${b['initial_capital']:,.0f}")
    print(f"{thin}")

    # Header
    cols = ["Metric", "Baseline (No Filter)"]
    for t in thresholds:
        cols.append(f"Filter >{t:.1f}%")
    header = "  " + "  ".join(f"{c:>22s}" for c in cols)
    print(header)
    print(thin)

    # Metrics to compare
    def fmt_row(label, baseline_val, filtered_vals, fmt_str="{:>22s}"):
        parts = [f"  {label:>22s}", fmt_str.format(baseline_val)]
        for v in filtered_vals:
            parts.append(fmt_str.format(v))
        print("  ".join(parts))

    # Collect filtered stats
    fstats = [results["filtered"][t].stats for t in thresholds]
    filt_info = [results["filter_stats"][t] for t in thresholds]

    # --- Rows ---
    fmt_row("Total Trades",
            f"{b['total_trades']}",
            [f"{f['total_trades']}" for f in fstats])

    fmt_row("Days Skipped (filter)",
            "0",
            [f"{fi['days_skipped']}" for fi in filt_info])

    fmt_row("Win Rate",
            f"{b['win_rate_pct']:.1f}%",
            [f"{f['win_rate_pct']:.1f}%" for f in fstats])

    print(thin)

    fmt_row("Gross P&L",
            f"${b['gross_pnl_usd']:+,.2f}",
            [f"${f['gross_pnl_usd']:+,.2f}" for f in fstats])

    fmt_row("Total Fees",
            f"${b['total_fee_usd']:+,.2f}",
            [f"${f['total_fee_usd']:+,.2f}" for f in fstats])

    fmt_row("Net P&L",
            f"${b['total_pnl_usd']:+,.2f}",
            [f"${f['total_pnl_usd']:+,.2f}" for f in fstats])

    fmt_row("Net Return",
            f"{b['total_return_pct']:+.1f}%",
            [f"{f['total_return_pct']:+.1f}%" for f in fstats])

    fmt_row("Final Equity",
            f"${b['final_equity']:,.2f}",
            [f"${f['final_equity']:,.2f}" for f in fstats])

    print(thin)

    fmt_row("Avg P&L/Trade",
            f"${b['avg_pnl_per_trade']:+.2f}",
            [f"${f['avg_pnl_per_trade']:+.2f}" for f in fstats])

    fmt_row("Profit Factor",
            f"{b['profit_factor']:.2f}",
            [f"{f['profit_factor']:.2f}" for f in fstats])

    fmt_row("Sharpe Ratio",
            f"{b['sharpe_ratio']:.2f}",
            [f"{f['sharpe_ratio']:.2f}" for f in fstats])

    fmt_row("Calmar Ratio",
            f"{b['calmar_ratio']:.2f}",
            [f"{f['calmar_ratio']:.2f}" for f in fstats])

    fmt_row("Max Drawdown",
            f"${b['max_drawdown_usd']:+,.2f} ({b['max_drawdown_pct']:.1f}%)",
            [f"${f['max_drawdown_usd']:+,.2f} ({f['max_drawdown_pct']:.1f}%)" for f in fstats])

    print(thin)

    fmt_row("SL Hit Count",
            f"{b['sl_hit_count']}",
            [f"{f['sl_hit_count']}" for f in fstats])

    fmt_row("Time Exit Count",
            f"{b['time_exit_count']}",
            [f"{f['time_exit_count']}" for f in fstats])

    fmt_row("Avg Hold Time",
            f"{b['avg_hold_minutes']:.1f} min",
            [f"{f['avg_hold_minutes']:.1f} min" for f in fstats])

    print(sep)

    # --- Improvement deltas ---
    print(f"\n  IMPROVEMENT vs BASELINE:")
    print(thin)
    for t in thresholds:
        f = results["filtered"][t].stats
        fi = results["filter_stats"][t]
        pnl_delta = f["total_pnl_usd"] - b["total_pnl_usd"]
        wr_delta = f["win_rate_pct"] - b["win_rate_pct"]
        dd_delta = f["max_drawdown_pct"] - b["max_drawdown_pct"]
        sharpe_delta = f["sharpe_ratio"] - b["sharpe_ratio"]
        sl_delta = f["sl_hit_count"] - b["sl_hit_count"]
        print(f"  Filter >{t:.1f}%:")
        print(f"    Days skipped   : {fi['days_skipped']} / {fi['total_days']} ({fi['days_skipped']/fi['total_days']*100:.1f}%)")
        print(f"    Net P&L change : ${pnl_delta:+,.2f}")
        print(f"    Win rate change: {wr_delta:+.1f}%")
        print(f"    SL hits change : {sl_delta:+d}")
        print(f"    Max DD change  : {dd_delta:+.1f}%")
        print(f"    Sharpe change  : {sharpe_delta:+.2f}")
        print()

    # --- Show which days were filtered ---
    for t in thresholds:
        fi = results["filter_stats"][t]
        if fi["details"]:
            print(f"\n  Days skipped by >{t:.1f}% filter:")
            print(f"  {'Date':>12s}  {'Spot (2h ago)':>14s}  {'Spot (entry)':>13s}  {'Move %':>8s}")
            print(f"  {'-'*12}  {'-'*14}  {'-'*13}  {'-'*8}")
            for d in fi["details"]:
                print(
                    f"  {str(d['date']):>12s}  "
                    f"${d['spot_lookback']:>12,.0f}  "
                    f"${d['spot_entry']:>11,.0f}  "
                    f"{d['move_pct']:>7.2f}%"
                )

    # --- Check what the baseline P&L was on those skipped days ---
    for t in thresholds:
        fi = results["filter_stats"][t]
        if fi["details"]:
            skipped_dates = {d["date"] for d in fi["details"]}
            b_df = baseline.trade_df
            skipped_trades = b_df[b_df["date"].dt.date.isin(skipped_dates)]
            if not skipped_trades.empty:
                total_skipped_pnl = skipped_trades["net_pnl_usd"].sum()
                avg_skipped_pnl = skipped_trades["net_pnl_usd"].mean()
                win_count = (skipped_trades["net_pnl_usd"] > 0).sum()
                loss_count = (skipped_trades["net_pnl_usd"] < 0).sum()
                sl_count = (skipped_trades["exit_reason"] == "sl_hit").sum()
                print(f"\n  Baseline P&L on days skipped by >{t:.1f}% filter:")
                print(f"    Traded days matched : {len(skipped_trades)} (of {len(fi['details'])} skipped)")
                print(f"    Total Net P&L       : ${total_skipped_pnl:+,.2f}")
                print(f"    Avg Net P&L/trade   : ${avg_skipped_pnl:+,.2f}")
                print(f"    Wins / Losses       : {win_count}W / {loss_count}L")
                print(f"    SL Hits on skipped  : {sl_count}")
                print()

    print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare BTC Short Straddle: Baseline vs Momentum Filter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,       metavar="YYYY-MM",
                   help="Run a single month (overrides --start/--end)")
    p.add_argument("--lot-size",  type=int,   default=LIVE_LOT_SIZE)
    p.add_argument("--sl-pct",    type=float, default=LIVE_SL_PCT)
    p.add_argument("--capital",   type=float, default=1_000.0)
    p.add_argument("--alloc-pct", type=float, default=None)
    p.add_argument("--threshold", type=float, nargs="+", default=[1.0],
                   help="Momentum threshold(s) in %%. Default: 1.0. "
                        "Pass multiple values to compare: --threshold 0.5 1.0 1.5 2.0")
    p.add_argument("--lookback",  type=float, default=2.0,
                   help="Lookback hours before entry (default: 2.0)")
    p.add_argument("--skip-weekends", action="store_true")
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    start = args.month if args.month else args.start
    end   = args.month if args.month else args.end

    alloc_pct = args.alloc_pct if args.alloc_pct is not None else LIVE_CAPITAL_ALLOC_PCT

    cfg = BacktestConfig(
        start_month=start,
        end_month=end,
        lot_size=args.lot_size,
        sl_pct=args.sl_pct,
        initial_capital=args.capital,
        capital_allocation_pct=alloc_pct,
        verbose=args.verbose,
    )

    thresholds = sorted(args.threshold)

    log.info("=" * 60)
    log.info("  MOMENTUM FILTER COMPARISON")
    log.info("  Period      : %s -> %s", cfg.start_month, cfg.end_month)
    log.info("  Capital     : $%s", f"{cfg.initial_capital:,.0f}")
    log.info("  SL          : %.0f%% of entry premium", cfg.sl_pct)
    log.info("  Thresholds  : %s", ", ".join(f"{t:.1f}%" for t in thresholds))
    log.info("  Lookback    : %.1f hours", args.lookback)
    log.info("  Entry/Exit  : %s / %s UTC", cfg.entry_time_utc, cfg.exit_time_utc)
    log.info("=" * 60)

    results = run_comparison(
        cfg, thresholds,
        lookback_hours=args.lookback,
        skip_weekends=args.skip_weekends,
    )

    print_comparison(results, thresholds)


if __name__ == "__main__":
    main()
