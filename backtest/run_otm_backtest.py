"""
backtest/run_otm_backtest.py
============================
Backtest: Far OTM Short Strangle vs ATM Short Straddle (side-by-side).

Sells the 10th OTM strike (call_strike = ATM + 10x$200, put_strike = ATM - 10x$200)
and compares results to the ATM straddle over the same period.

Usage:
  python backtest/run_otm_backtest.py
  python backtest/run_otm_backtest.py --month 2025-06
  python backtest/run_otm_backtest.py --otm-steps 10 --start 2025-01 --end 2026-06
  python backtest/run_otm_backtest.py --sl-pct 200

Options:
  --start      Start month YYYY-MM  (default: 2025-01)
  --end        End month YYYY-MM    (default: 2026-06)
  --month      Single month YYYY-MM (overrides --start/--end)
  --otm-steps  Number of $200 strike steps OTM (default: 10)
  --lot-size   Contracts per leg    (default: from config)
  --sl-pct     Stop-loss as % of entry premium (default: 9999 = disabled)
  --capital    Initial capital USD  (default: 1000)
  --verbose    Enable DEBUG logging
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR, LIVE_LOT_SIZE, LIVE_SL_PCT
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, ShortStrangleEngine
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OTM Short Strangle vs ATM Short Straddle Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,      metavar="YYYY-MM",
                   help="Run a single month (overrides --start/--end)")
    p.add_argument("--otm-steps", type=int,   default=10,
                   help="Number of $200 strike steps OTM (default: 10 = $2,000 away from spot)")
    p.add_argument("--lot-size",  type=int,   default=LIVE_LOT_SIZE)
    p.add_argument("--sl-pct",    type=float, default=LIVE_SL_PCT)
    p.add_argument("--capital",   type=float, default=1_000.0)
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args()


def print_comparison(atm_port: Portfolio, otm_port: Portfolio, otm_steps: int) -> None:
    """Print a side-by-side comparison table of the two strategies."""
    a = atm_port.stats
    o = otm_port.stats
    otm_distance = otm_steps * 200
    sep = "-" * 72
    col = 22

    def row(label: str, a_val, o_val) -> None:
        print(f"  {label:<28}  {str(a_val):>{col}}  {str(o_val):>{col}}")

    print()
    print("=" * 72)
    print("  STRATEGY COMPARISON: ATM Straddle  vs  OTM Strangle")
    print(f"  OTM distance: {otm_steps} strikes x $200 = ${otm_distance:,} from spot per leg")
    print("=" * 72)
    print(f"  {'Metric':<28}  {'ATM Straddle':>{col}}  {f'OTM (+{otm_steps}/-{otm_steps})':>{col}}")
    print(sep)
    row("Trades",             a.get("total_trades", "--"),   o.get("total_trades", "--"))
    row("Skipped days",       a.get("skipped_days",  "--"),  o.get("skipped_days",  "--"))
    print(sep)
    row("Win rate",           f"{a.get('win_rate_pct', 0):.1f}%",       f"{o.get('win_rate_pct', 0):.1f}%")
    row("Avg daily P&L",      f"${a.get('avg_pnl_per_trade', 0):+.2f}", f"${o.get('avg_pnl_per_trade', 0):+.2f}")
    row("Total net P&L",      f"${a.get('total_pnl_usd', 0):+.2f}",     f"${o.get('total_pnl_usd', 0):+.2f}")
    row("Gross P&L",          f"${a.get('gross_pnl_usd', 0):+.2f}",     f"${o.get('gross_pnl_usd', 0):+.2f}")
    row("Best day",           f"${a.get('max_win_usd', 0):+.2f}",       f"${o.get('max_win_usd', 0):+.2f}")
    row("Worst day",          f"${a.get('max_loss_usd', 0):+.2f}",      f"${o.get('max_loss_usd', 0):+.2f}")
    row("Profit factor",      f"{a.get('profit_factor', 0):.2f}",       f"{o.get('profit_factor', 0):.2f}")
    print(sep)
    row("Max drawdown",       f"${a.get('max_drawdown_usd', 0):.2f}",   f"${o.get('max_drawdown_usd', 0):.2f}")
    row("Sharpe ratio",       f"{a.get('sharpe_ratio', 0):.2f}",        f"{o.get('sharpe_ratio', 0):.2f}")
    row("Calmar ratio",       f"{a.get('calmar_ratio', 0):.2f}",        f"{o.get('calmar_ratio', 0):.2f}")
    print(sep)
    row("Avg entry premium",  f"${a.get('avg_entry_premium', 0):.2f}",  f"${o.get('avg_entry_premium', 0):.2f}")
    row("SL hits",            a.get("sl_hit_count", 0),                 o.get("sl_hit_count", 0))
    row("Total fees",         f"${a.get('total_fee_usd', 0):.2f}",      f"${o.get('total_fee_usd', 0):.2f}")
    print("=" * 72)
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
        lot_size        = args.lot_size,
        sl_pct          = args.sl_pct,
        initial_capital = args.capital,
        verbose         = args.verbose,
    )

    atm_cfg = BacktestConfig(**base_cfg, otm_steps=0)
    otm_cfg = BacktestConfig(**base_cfg, otm_steps=args.otm_steps)

    log.info("=" * 60)
    log.info("  OTM Strangle vs ATM Straddle Backtest")
    log.info("  Period    : %s to %s", start, end)
    log.info("  Capital   : $%s", f"{args.capital:,.0f}")
    log.info("  Lot size  : %s",
             f"Dynamic ({atm_cfg.capital_allocation_pct:.0f}% of equity, {atm_cfg.leverage:.0f}x leverage)"
             if atm_cfg.use_dynamic_lot_size else f"{args.lot_size} contracts/leg (static)")
    log.info("  SL        : %.0f%% of entry premium", args.sl_pct)
    log.info("  OTM steps : %d ($%d from spot per leg)", args.otm_steps, args.otm_steps * 200)
    log.info("  Entry/Exit: %s / %s UTC", atm_cfg.entry_time_utc, atm_cfg.exit_time_utc)
    log.info("=" * 60)

    atm_engine = ShortStraddleEngine(atm_cfg)
    otm_engine = ShortStrangleEngine(otm_cfg)

    # Single pass — run both strategies on every day simultaneously
    log.info("Running both strategies over all trading days ...")
    day_count = 0
    for trade_date, day_df in iter_trading_days(atm_cfg):
        day_count += 1
        atm_engine.run_day(trade_date, day_df)
        otm_engine.run_day(trade_date, day_df)

    log.info("-" * 60)
    log.info(
        "Done: %d calendar days | ATM: %d trades / %d skipped | OTM: %d trades / %d skipped",
        day_count,
        len(atm_engine.trades), len(atm_engine.skipped),
        len(otm_engine.trades), len(otm_engine.skipped),
    )

    if not atm_engine.trades and not otm_engine.trades:
        log.error("No trades generated -- check data path and date range")
        sys.exit(1)

    atm_port = Portfolio(atm_cfg, atm_engine.trades)
    otm_port = Portfolio(otm_cfg, otm_engine.trades)

    # Attach skipped counts so comparison table can show them
    atm_port.stats["skipped_days"] = len(atm_engine.skipped)
    otm_port.stats["skipped_days"] = len(otm_engine.skipped)

    print_comparison(atm_port, otm_port, args.otm_steps)

    print("---- ATM Straddle ----")
    atm_port.print_summary()

    print()
    print(f"---- OTM Strangle (+{args.otm_steps}/-{args.otm_steps} = +/-${args.otm_steps * 200:,} from spot) ----")
    otm_port.print_summary()

    # Save HTML reports
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    if atm_engine.trades:
        atm_dir  = REPORTS_DIR / f"atm_straddle_{start}_{end}_{ts_str}"
        atm_html = ReportGenerator(atm_cfg, atm_port).generate(atm_dir)
        log.info("ATM report saved: %s", atm_html)
        print(f"\n  ATM report  : {atm_html}")

    if otm_engine.trades:
        otm_dir  = REPORTS_DIR / f"otm_strangle_{args.otm_steps}steps_{start}_{end}_{ts_str}"
        otm_html = ReportGenerator(otm_cfg, otm_port).generate(otm_dir)
        log.info("OTM report saved: %s", otm_html)
        print(f"  OTM report  : {otm_html}\n")


if __name__ == "__main__":
    main()
