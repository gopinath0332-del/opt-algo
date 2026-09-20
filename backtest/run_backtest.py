"""
backtest/run_backtest.py
========================
Main CLI entry point for the BTC short-straddle backtest.

Usage examples:
  # Full backtest (Jan 2025 - Jun 2026)
  python backtest/run_backtest.py

  # Single month quick test
  python backtest/run_backtest.py --month 2025-01

  # Custom range and parameters
  python backtest/run_backtest.py --start 2025-06 --end 2025-12 --lot-size 100 --sl-pct 40

Options:
  --start    Start month YYYY-MM  (default: 2025-01)
  --end      End   month YYYY-MM  (default: 2026-06)
  --month    Single month YYYY-MM (overrides --start / --end)
  --lot-size Contracts per leg    (default: 150)
  --sl-pct   Stop-loss %          (default: 50)
  --capital  Initial capital USD  (default: 1000)
  --verbose  Enable DEBUG logging
"""

from __future__ import annotations

import argparse
import logging
import sys
import io
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows so log messages with unicode print cleanly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow running from the opt-algo root: python backtest/run_backtest.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR, LIVE_LOT_SIZE, LIVE_SL_PCT
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BTC Short Straddle Options Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",    default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",      default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",    default=None,       metavar="YYYY-MM",
                   help="Run a single month (overrides --start/--end)")
    p.add_argument("--lot-size", type=int,   default=LIVE_LOT_SIZE)
    p.add_argument("--sl-pct",   type=float, default=LIVE_SL_PCT)
    p.add_argument("--capital",  type=float, default=1_000.0)
    p.add_argument("--alloc-pct", type=float, default=None, help="Capital allocation percentage (overrides settings.yaml)")
    p.add_argument("--verbose",  action="store_true")
    p.add_argument("--skip-weekends", action="store_true", help="Skip trades on Saturday and Sunday")
    p.add_argument("--no-momentum-filter", action="store_true", help="Disable pre-entry momentum filter")
    p.add_argument("--no-big-leg-skip",    action="store_true", help="Disable big-leg asymmetry skip filter")
    p.add_argument("--min-premium",        type=float, default=None, help="Minimum entry premium threshold ($)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    log = logging.getLogger(__name__)

    start = args.month if args.month else args.start
    end   = args.month if args.month else args.end

    from backtest.config import LIVE_CAPITAL_ALLOC_PCT
    alloc_pct = args.alloc_pct if args.alloc_pct is not None else LIVE_CAPITAL_ALLOC_PCT

    cfg_kwargs = {
        "start_month": start,
        "end_month": end,
        "lot_size": args.lot_size,
        "sl_pct": args.sl_pct,
        "initial_capital": args.capital,
        "capital_allocation_pct": alloc_pct,
        "verbose": args.verbose,
    }
    if args.no_momentum_filter:
        cfg_kwargs["momentum_filter_enabled"] = False
    if args.no_big_leg_skip:
        cfg_kwargs["big_leg_skip_ratio"] = 0.0
    if args.min_premium is not None:
        cfg_kwargs["min_entry_premium"] = args.min_premium

    cfg = BacktestConfig(**cfg_kwargs)

    log.info("=" * 60)
    log.info("  BTC Short Straddle Backtest")
    log.info("  Period    : %s -> %s", cfg.start_month, cfg.end_month)
    log.info("  Capital   : $%s", f"{cfg.initial_capital:,.0f}")
    log.info("  Lot size  : %s",
             f"Dynamic ({cfg.capital_allocation_pct:.0f}% of equity, {cfg.leverage:.0f}x leverage)"
             if cfg.use_dynamic_lot_size else f"{cfg.lot_size} contracts/leg (static)")
    log.info("  SL        : %.0f%% of entry premium", cfg.sl_pct)
    log.info("  Entry/Exit: %s / %s UTC", cfg.entry_time_utc, cfg.exit_time_utc)
    log.info(
        "  Filters   : Momentum=%s (%.1fh, %.1f%%), BigLegSkip=%s, MinPremium=%s",
        "ON" if cfg.momentum_filter_enabled else "OFF",
        cfg.momentum_lookback_hours,
        cfg.momentum_threshold_pct,
        f"{cfg.big_leg_skip_ratio:.0f}x" if cfg.big_leg_skip_ratio > 0 else "OFF",
        f"${cfg.min_entry_premium:.0f}" if cfg.min_entry_premium > 0 else "OFF",
    )
    log.info("=" * 60)

    engine    = ShortStraddleEngine(cfg)
    day_count = 0

    for trade_date, day_df in iter_trading_days(cfg):
        day_count += 1
        if args.skip_weekends and trade_date.weekday() in (5, 6):
            engine._skip(trade_date, "weekend trade (Saturday/Sunday)")
            continue
        engine.run_day(trade_date, day_df)

    log.info("-" * 60)
    log.info(
        "Done: %d calendar days | %d trades | %d skipped",
        day_count, len(engine.trades), len(engine.skipped),
    )
    if engine.skipped:
        from collections import Counter
        def _clean_reason(r: str) -> str:
            if r.startswith("low premium"):
                return "low entry premium"
            if r.startswith("big-leg ratio"):
                return "big-leg asymmetry ratio"
            return r.split(" (")[0]

        skip_counts = Counter(_clean_reason(s.reason) for s in engine.skipped)
        for reason, count in skip_counts.most_common():
            log.info("  Skipped: %3d days -> %s", count, reason)

    if not engine.trades:
        log.error("No trades generated -- check data path and date range")
        sys.exit(1)

    portfolio = Portfolio(cfg, engine.trades)
    portfolio.print_summary()

    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = REPORTS_DIR / f"short_straddle_{start}_{end}_{ts_str}"
    gen     = ReportGenerator(cfg, portfolio)
    html    = gen.generate(out_dir)

    log.info("\n  Report -> %s\n", html)
    print(f"\n  Open report: {html}\n")


if __name__ == "__main__":
    main()
