"""
backtest/run_1300_atm_comparison.py
====================================
Backtest & Comparison:
  Baseline Strategy : Current Config (17:00 IST / 11:30 UTC entry, ATM Short Straddle, Expiry exit, No SL)
  New Idea Strategy : 13:00 IST / 07:30 UTC entry, ATM Short Straddle (Call=ATM, Put=ATM), Expiry exit, No SL

Usage:
  python backtest/run_1300_atm_comparison.py
  python backtest/run_1300_atm_comparison.py --month 2025-01
  python backtest/run_1300_atm_comparison.py --start 2025-01 --end 2026-06
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR, LIVE_LOT_SIZE, LIVE_CAPITAL_ALLOC_PCT, LIVE_LEVERAGE
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator

# Time conversions (IST -> UTC, IST = UTC+5:30)
# 13:00 IST = 07:30 UTC
# 17:00 IST = 11:30 UTC
# 17:30 IST = 12:00 UTC (expiry / settlement)
ENTRY_1300_UTC = time(7, 30)
ENTRY_1700_UTC = time(11, 30)
EXPIRY_EXIT_UTC = time(12, 0)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest & Compare 13:00 IST ATM Straddle vs Current Config (17:00 IST ATM Straddle)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,      metavar="YYYY-MM",
                   help="Run a single month (overrides --start/--end)")
    p.add_argument("--sl-pct",    type=float, default=9999.0,
                   help="Stop-loss %% of entry premium (default: 9999 = no stoploss / let expire)")
    p.add_argument("--capital",   type=float, default=1_000.0)
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args()


def print_comparison(current_port: Portfolio, new_port: Portfolio) -> None:
    """Print a side-by-side comparison table of Current Config vs New Idea (13:00 ATM)."""
    c = current_port.stats
    n = new_port.stats
    sep = "-" * 80
    col = 24

    def row(label: str, c_val, n_val) -> None:
        print(f"  {label:<30}  {str(c_val):>{col}}  {str(n_val):>{col}}")

    print()
    print("=" * 80)
    print("  STRATEGY COMPARISON: Current Configuration  vs  New Idea (13:00 IST ATM)")
    print(f"  Current Config : Entry 17:00 IST (11:30 UTC), ATM Straddle, Expiry Exit, No SL")
    print(f"  New Idea       : Entry 13:00 IST (07:30 UTC), ATM Straddle, Expiry Exit, No SL")
    print("=" * 80)
    print(f"  {'Metric':<30}  {'Current Config (17:00 ATM)':>{col}}  {'New Idea (13:00 ATM)':>{col}}")
    print(sep)
    row("Total Trades",        c.get("total_trades", "--"),   n.get("total_trades", "--"))
    row("Skipped days",        c.get("skipped_days",  "--"),  n.get("skipped_days",  "--"))
    print(sep)
    row("Win rate",            f"{c.get('win_rate_pct', 0):.1f}%",       f"{n.get('win_rate_pct', 0):.1f}%")
    row("Avg daily P&L",       f"${c.get('avg_pnl_per_trade', 0):+.2f}", f"${n.get('avg_pnl_per_trade', 0):+.2f}")
    row("Total net P&L",       f"${c.get('total_pnl_usd', 0):+.2f}",     f"${n.get('total_pnl_usd', 0):+.2f}")
    row("Gross P&L",           f"${c.get('gross_pnl_usd', 0):+.2f}",     f"${n.get('gross_pnl_usd', 0):+.2f}")
    row("Best day",            f"${c.get('max_win_usd', 0):+.2f}",       f"${n.get('max_win_usd', 0):+.2f}")
    row("Worst day",           f"${c.get('max_loss_usd', 0):+.2f}",      f"${n.get('max_loss_usd', 0):+.2f}")
    row("Profit factor",       f"{c.get('profit_factor', 0):.2f}",       f"{n.get('profit_factor', 0):.2f}")
    print(sep)
    row("Max drawdown",        f"${c.get('max_drawdown_usd', 0):.2f}",   f"${n.get('max_drawdown_usd', 0):.2f}")
    row("Sharpe ratio",        f"{c.get('sharpe_ratio', 0):.2f}",        f"{n.get('sharpe_ratio', 0):.2f}")
    row("Calmar ratio",        f"{c.get('calmar_ratio', 0):.2f}",        f"{n.get('calmar_ratio', 0):.2f}")
    print(sep)
    row("Avg entry premium",   f"${c.get('avg_entry_premium', 0):.2f}",  f"${n.get('avg_entry_premium', 0):.2f}")
    row("Total fees",          f"${c.get('total_fee_usd', 0):.2f}",      f"${n.get('total_fee_usd', 0):.2f}")
    print("=" * 80)
    print()


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

    base_cfg = dict(
        start_month     = start,
        end_month       = end,
        sl_pct          = args.sl_pct,
        initial_capital = args.capital,
        verbose         = args.verbose,
    )

    # Current Configuration: 17:00 IST entry, ATM Straddle (otm_steps=0), Expiry Exit (12:00 UTC)
    current_cfg = BacktestConfig(
        **base_cfg,
        entry_time_utc = ENTRY_1700_UTC,
        exit_time_utc  = EXPIRY_EXIT_UTC,
        otm_steps      = 0,
    )

    # New Idea Strategy: 13:00 IST entry, ATM Straddle (otm_steps=0), Expiry Exit (12:00 UTC)
    new_cfg = BacktestConfig(
        **base_cfg,
        entry_time_utc = ENTRY_1300_UTC,
        exit_time_utc  = EXPIRY_EXIT_UTC,
        otm_steps      = 0,
    )

    log.info("=" * 60)
    log.info("  Backtest & Compare: Current Config vs 13:00 IST ATM Straddle")
    log.info("  Period      : %s to %s", start, end)
    log.info("  Capital     : $%s", f"{args.capital:,.0f}")
    log.info("  Current     : Entry 17:00 IST (11:30 UTC) | ATM Straddle | Expiry Exit | No SL")
    log.info("  New Idea    : Entry 13:00 IST (07:30 UTC) | ATM Straddle | Expiry Exit | No SL")
    log.info("=" * 60)

    current_engine = ShortStraddleEngine(current_cfg)
    new_engine     = ShortStraddleEngine(new_cfg)

    log.info("Running backtests on all trading days...")
    day_count = 0

    for trade_date, day_df in iter_trading_days(current_cfg):
        day_count += 1
        current_engine.run_day(trade_date, day_df)
        new_engine.run_day(trade_date, day_df)

    log.info("-" * 60)
    log.info(
        "Done: %d calendar days | Current: %d trades / %d skipped | New Idea: %d trades / %d skipped",
        day_count,
        len(current_engine.trades), len(current_engine.skipped),
        len(new_engine.trades), len(new_engine.skipped),
    )

    if not current_engine.trades and not new_engine.trades:
        log.error("No trades generated -- check data path and date range")
        sys.exit(1)

    current_port = Portfolio(current_cfg, current_engine.trades)
    new_port     = Portfolio(new_cfg, new_engine.trades)

    current_port.stats["skipped_days"] = len(current_engine.skipped)
    new_port.stats["skipped_days"]     = len(new_engine.skipped)

    print_comparison(current_port, new_port)

    print("---- Current Config (17:00 IST ATM Straddle) ----")
    current_port.print_summary()

    print()
    print("---- New Idea (13:00 IST ATM Straddle) ----")
    new_port.print_summary()

    # Save HTML reports
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    if current_engine.trades:
        c_dir  = REPORTS_DIR / f"current_config_1700_atm_{start}_{end}_{ts_str}"
        c_html = ReportGenerator(current_cfg, current_port).generate(c_dir)
        log.info("Current Config report saved: %s", c_html)
        print(f"\n  Current Config report  : {c_html}")

    if new_engine.trades:
        n_dir  = REPORTS_DIR / f"new_idea_1300_atm_{start}_{end}_{ts_str}"
        n_html = ReportGenerator(new_cfg, new_port).generate(n_dir)
        log.info("New Idea report saved: %s", n_html)
        print(f"  New Idea report        : {n_html}\n")


if __name__ == "__main__":
    main()
