"""
backtest/run_momentum_straddle_backtest.py
==========================================
Backtest for the BTC Momentum-Triggered Short Straddle strategy.

Simulates the btc_momentum_straddle live strategy:
  - Entry window : 16:00 - 17:30 IST  (10:30 - 12:00 UTC)
  - Trigger      : BTC moves >=0.5% from session baseline before entering
  - Stop-loss    : Per-leg 100% (either leg doubles -> close both)
  - Exit         : 17:30 IST if SL not hit

Also runs the standard 17:00 IST straddle as a baseline for comparison.

Usage:
  # Full dataset (Jan 2025 - Jun 2026)
  python backtest/run_momentum_straddle_backtest.py

  # Single month
  python backtest/run_momentum_straddle_backtest.py --month 2025-06

  # Custom threshold / SL
  python backtest/run_momentum_straddle_backtest.py --threshold 0.3 --sl-pct 100 --capital 1000

Options:
  --start     Start month YYYY-MM  (default: 2025-01)
  --end       End   month YYYY-MM  (default: 2026-06)
  --month     Single month YYYY-MM (overrides --start/--end)
  --threshold Momentum trigger % move  (default: 0.5)
  --sl-pct    Per-leg SL percentage    (default: 100)
  --capital   Initial capital USD      (default: 1000)
  --verbose   Enable DEBUG logging
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from pathlib import Path
from typing import List
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, MomentumStraddleEngine, MomentumTradeResult
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator

log = logging.getLogger(__name__)

# 16:00 IST = 10:30 UTC  (momentum watch window start / session open baseline)
# 17:00 IST = 11:30 UTC  (standard straddle entry)
# 17:30 IST = 12:00 UTC  (exit / settlement for both strategies)
MOMENTUM_ENTRY_UTC = time(10, 30)
STANDARD_ENTRY_UTC = time(11, 30)
EXIT_UTC           = time(12, 0)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BTC Momentum Short Straddle Backtest (vs baseline 17:00 straddle)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,       metavar="YYYY-MM",
                   help="Run single month (overrides --start/--end)")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="Momentum trigger threshold %% (default 0.5)")
    p.add_argument("--sl-pct",    type=float, default=100.0,
                   help="Per-leg SL %% (default 100 = mark must double)")
    p.add_argument("--capital",   type=float, default=1_000.0,
                   help="Initial capital USD (default 1000)")
    p.add_argument("--alloc-pct", type=float, default=40.0,
                   help="Capital allocation %% (default 40)")
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args()


def _metrics(trades, initial_capital: float) -> dict:
    if not trades:
        return {}
    pnl    = [t.net_pnl_usd for t in trades]
    gross  = [t.pnl_usd     for t in trades]
    wins   = [p for p in pnl if p > 0]
    sl_hits = [t for t in trades if t.exit_reason == "sl_hit"]

    total_pnl  = sum(pnl)
    win_rate   = len(wins) / len(trades) * 100
    avg_pnl    = total_pnl / len(trades)
    net_return = total_pnl / initial_capital * 100

    equity = [initial_capital]
    for p in pnl:
        equity.append(equity[-1] + p)
    equity = np.array(equity)
    peak   = np.maximum.accumulate(equity)
    dd     = equity - peak
    max_dd     = float(dd.min())
    max_dd_pct = float((dd / peak).min() * 100)

    arr    = np.array(pnl)
    sharpe = float(arr.mean() / arr.std() * np.sqrt(365)) if arr.std() > 0 else 0.0

    calmar = (total_pnl / abs(max_dd)) if max_dd < 0 else float("inf")

    gross_profit = sum(g for g in gross if g > 0)
    gross_loss   = abs(sum(g for g in gross if g < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    hold_mins = [
        (t.exit_ts - t.entry_ts).total_seconds() / 60
        for t in trades if hasattr(t, "exit_ts") and hasattr(t, "entry_ts")
    ]
    avg_hold = float(np.mean(hold_mins)) if hold_mins else 0.0

    return dict(
        trades=len(trades), win_rate=win_rate, sl_hits=len(sl_hits),
        total_pnl=total_pnl, net_return=net_return, avg_pnl=avg_pnl,
        max_dd=max_dd, max_dd_pct=max_dd_pct,
        sharpe=sharpe, calmar=calmar, profit_factor=profit_factor,
        avg_hold_min=avg_hold,
    )


def _momentum_extras(trades: List[MomentumTradeResult]) -> dict:
    if not trades:
        return {}
    trigger_mins = []
    for t in trades:
        if t.trigger_ts is not None:
            mins = (t.trigger_ts.hour - 10) * 60 + (t.trigger_ts.minute - 30)
            trigger_mins.append(mins)
    ce_hits = sum(1 for t in trades if t.sl_hit_leg == "CE")
    pe_hits = sum(1 for t in trades if t.sl_hit_leg == "PE")
    avg_trigger_min = float(np.mean(trigger_mins)) if trigger_mins else 0.0
    avg_move        = float(np.mean([t.trigger_move_pct for t in trades]))
    return dict(
        ce_sl_hits=ce_hits, pe_sl_hits=pe_hits,
        avg_trigger_min=avg_trigger_min, avg_move_pct=avg_move,
    )


def _print_comparison(mom_m, base_m, mom_extra, total_days, triggered_days):
    W = 72
    print("=" * W)
    print(f"{'METRIC':<32} {'MOMENTUM STRADDLE':>18} {'BASELINE 17:00':>18}")
    print("-" * W)

    def row(label, key, fmt=".2f", suffix=""):
        mv = mom_m.get(key, 0)
        bv = base_m.get(key, 0)
        print(f"  {label:<30} {f'{mv:{fmt}}{suffix}':>18} {f'{bv:{fmt}}{suffix}':>18}")

    row("Total Trades",     "trades",        ".0f")
    row("Win Rate",         "win_rate",      ".1f", "%")
    row("SL Hits",          "sl_hits",       ".0f")
    row("Total Net P&L",    "total_pnl",     ".2f", " USD")
    row("Net Return",       "net_return",    ".2f", "%")
    row("Avg P&L / Trade",  "avg_pnl",       ".2f", " USD")
    row("Max Drawdown",     "max_dd",        ".2f", " USD")
    row("Max Drawdown %",   "max_dd_pct",    ".2f", "%")
    row("Sharpe Ratio",     "sharpe",        ".3f")
    row("Calmar Ratio",     "calmar",        ".3f")
    row("Profit Factor",    "profit_factor", ".3f")
    row("Avg Hold (min)",   "avg_hold_min",  ".1f")
    print("-" * W)

    trig_rate = triggered_days / total_days * 100 if total_days else 0
    print(f"\n  {'Total trading days':<30} {total_days:>18}")
    print(f"  {'Momentum triggered days':<30} {triggered_days:>18}")
    print(f"  {'Trigger rate':<30} {trig_rate:>17.1f}%")
    if mom_extra:
        amin = mom_extra.get("avg_trigger_min", 0)
        ah   = 16 + (30 + int(amin)) // 60
        am   = (30 + int(amin)) % 60
        print(f"  {'Avg trigger time (IST)':<30} {f'{ah:02d}:{am:02d} IST':>18}")
        print(f"  {'Avg trigger move':<30} {mom_extra['avg_move_pct']:>17.3f}%")
        print(f"  {'CE leg SL hits':<30} {mom_extra['ce_sl_hits']:>18}")
        print(f"  {'PE leg SL hits':<30} {mom_extra['pe_sl_hits']:>18}")
    print("=" * W)


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

    log.info("=" * 72)
    log.info("  BTC Momentum Short Straddle Backtest")
    log.info("  Period    : %s -> %s", start, end)
    log.info("  Capital   : $%s | Alloc: %.0f%%", f"{args.capital:,.0f}", args.alloc_pct)
    log.info("  Threshold : %.2f%%  |  Per-leg SL: %.0f%%", args.threshold, args.sl_pct)
    log.info("  Window    : 16:00 -> 17:30 IST")
    log.info("=" * 72)

    mom_cfg = BacktestConfig(
        start_month=start, end_month=end,
        entry_time_utc=MOMENTUM_ENTRY_UTC,
        exit_time_utc=EXIT_UTC,
        sl_pct=args.sl_pct,
        sl_per_leg=True,
        momentum_threshold_pct=args.threshold,
        initial_capital=args.capital,
        capital_allocation_pct=args.alloc_pct,
        verbose=args.verbose,
    )

    base_cfg = BacktestConfig(
        start_month=start, end_month=end,
        entry_time_utc=STANDARD_ENTRY_UTC,
        exit_time_utc=EXIT_UTC,
        sl_pct=9999.0,
        sl_per_leg=False,
        initial_capital=args.capital,
        capital_allocation_pct=args.alloc_pct,
        verbose=args.verbose,
    )

    mom_engine  = MomentumStraddleEngine(mom_cfg)
    base_engine = ShortStraddleEngine(base_cfg)

    total_days = 0
    triggered_days = 0

    log.info("\n--- Momentum Strategy (16:00-17:30 IST, %.2f%% trigger) ---", args.threshold)
    for trade_date, day_df in iter_trading_days(mom_cfg):
        total_days += 1
        result = mom_engine.run_day(trade_date, day_df)
        if result is not None:
            triggered_days += 1

    log.info("\n--- Baseline Strategy (17:00 IST, no SL) ---")
    for trade_date, day_df in iter_trading_days(base_cfg):
        base_engine.run_day(trade_date, day_df)

    log.info("-" * 72)
    no_trigger_count = sum(1 for s in mom_engine.skipped if "momentum trigger" in s.reason)
    log.info(
        "Momentum: %d days | %d trades | %d skipped (%d no-trigger)",
        total_days, len(mom_engine.trades), len(mom_engine.skipped), no_trigger_count,
    )
    log.info("Baseline: %d trades | %d skipped", len(base_engine.trades), len(base_engine.skipped))

    if not mom_engine.trades and not base_engine.trades:
        log.error("No trades generated -- check data path and date range")
        sys.exit(1)

    mom_m    = _metrics(mom_engine.trades,  args.capital)
    base_m   = _metrics(base_engine.trades, args.capital)
    mom_extra = _momentum_extras(mom_engine.trades)

    _print_comparison(mom_m, base_m, mom_extra, total_days, triggered_days)

    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = REPORTS_DIR / f"momentum_straddle_{start}_{end}_{ts_str}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if mom_engine.trades:
        mom_portfolio = Portfolio(mom_cfg, mom_engine.trades)
        mom_portfolio.print_summary()
        gen  = ReportGenerator(mom_cfg, mom_portfolio)
        html = gen.generate(out_dir / "momentum")
        print(f"\n  Open momentum report: {html}")

    if base_engine.trades:
        base_portfolio = Portfolio(base_cfg, base_engine.trades)
        gen  = ReportGenerator(base_cfg, base_portfolio)
        html = gen.generate(out_dir / "baseline")
        print(f"  Open baseline report : {html}")


if __name__ == "__main__":
    main()
