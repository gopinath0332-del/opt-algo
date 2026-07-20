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

    cfg = BacktestConfig(
        start_month     = start,
        end_month       = end,
        lot_size        = args.lot_size,
        sl_pct          = args.sl_pct,
        initial_capital = args.capital,
        capital_allocation_pct = alloc_pct,
        verbose         = args.verbose,
    )

    log.info("=" * 60)
    log.info("  BTC Short Straddle Backtest")
    log.info("  Period    : %s -> %s", cfg.start_month, cfg.end_month)
    log.info("  Capital   : $%s", f"{cfg.initial_capital:,.0f}")
    log.info("  Lot size  : %s",
             f"Dynamic ({cfg.capital_allocation_pct:.0f}% of equity, {cfg.leverage:.0f}x leverage)"
             if cfg.use_dynamic_lot_size else f"{cfg.lot_size} contracts/leg (static)")
    log.info("  SL        : %.0f%% of entry premium", cfg.sl_pct)
    log.info("  Entry/Exit: %s / %s UTC", cfg.entry_time_utc, cfg.exit_time_utc)
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
