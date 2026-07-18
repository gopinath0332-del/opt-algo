"""
backtest/run_otm15_backtest.py
==============================
Backtest: OTM15 Short Straddle.

Sells the 15th OTM strike for both Call and Put legs:
  call_strike = ATM + 15 × $200 = ATM + $3,000
  put_strike  = ATM − 15 × $200 = ATM − $3,000

Timing (IST-based, converted to UTC internally):
  Entry : 09:00 IST  = 03:30 UTC  (hardcoded)
  Exit  : configurable via --exit-time HH:MM (IST), default 17:25 IST = 11:55 UTC

Default parameters:
  SL      : 50% of combined entry premium  (use 9999 to disable)
  Capital : $1,000 USD
  Dataset : D:\\Workspace\\crypto-backtest-data\\options

Usage:
  python backtest/run_otm15_backtest.py
  python backtest/run_otm15_backtest.py --month 2025-06
  python backtest/run_otm15_backtest.py --start 2025-01 --end 2026-06
  python backtest/run_otm15_backtest.py --otm-steps 15 --sl-pct 50 --capital 1000
  python backtest/run_otm15_backtest.py --exit-time 17:30 --sl-pct 9999   # settlement, no SL
  python backtest/run_otm15_backtest.py --verbose

Options:
  --start      Start month YYYY-MM          (default: 2025-01)
  --end        End month YYYY-MM            (default: 2026-06)
  --month      Single month YYYY-MM         (overrides --start/--end)
  --otm-steps  Strike steps OTM            (default: 15 = $3,000 from ATM per leg)
  --exit-time  Exit time HH:MM in IST      (default: 17:25)
  --sl-pct     Stop-loss %% of premium     (default: 50; use 9999 to disable)
  --capital    Initial capital USD          (default: 1000)
  --lot-size   Fixed contracts per leg     (default: dynamic sizing)
  --verbose    Enable DEBUG logging
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

from backtest.config import BacktestConfig, REPORTS_DIR, LIVE_LOT_SIZE
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStrangleEngine
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator


# ---------------------------------------------------------------------------
# Timing constants — 9:00 AM IST entry (IST = UTC+5:30)
# ---------------------------------------------------------------------------
# 09:00 IST − 05:30 = 03:30 UTC  (hardcoded; entry never changes)
ENTRY_TIME_UTC = time(3, 30)

IST_OFFSET_MINUTES = 330   # UTC+5:30

# Default OTM steps for this strategy
DEFAULT_OTM_STEPS = 15

# Default SL: 50% of combined entry premium
DEFAULT_SL_PCT = 50.0

# Default exit time (IST string)
DEFAULT_EXIT_TIME_IST = "17:25"


def _ist_to_utc(ist_str: str) -> time:
    """Convert 'HH:MM' IST string to UTC time object."""
    h, m = map(int, ist_str.split(":"))
    total = h * 60 + m - IST_OFFSET_MINUTES
    if total < 0:
        total += 24 * 60
    return time((total // 60) % 24, total % 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OTM15 Short Straddle Backtest (9AM IST entry, configurable exit)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM",
                   help="Start month (default: 2025-01)")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM",
                   help="End month (default: 2026-06)")
    p.add_argument("--month",     default=None, metavar="YYYY-MM",
                   help="Single month — overrides --start/--end")
    p.add_argument("--otm-steps", type=int,   default=DEFAULT_OTM_STEPS,
                   help=f"OTM strike steps (default: {DEFAULT_OTM_STEPS} = $3,000 from ATM per leg)")
    p.add_argument("--exit-time", default=DEFAULT_EXIT_TIME_IST, metavar="HH:MM",
                   help=f"Exit time in IST HH:MM (default: {DEFAULT_EXIT_TIME_IST}; use 17:30 for settlement)")
    p.add_argument("--sl-pct",    type=float, default=DEFAULT_SL_PCT,
                   help=f"Stop-loss as %% of entry premium (default: {DEFAULT_SL_PCT}; use 9999 to disable)")
    p.add_argument("--capital",   type=float, default=1_000.0,
                   help="Initial capital USD (default: 1000)")
    p.add_argument("--lot-size",  type=int,   default=None,
                   help="Fixed contracts per leg (default: dynamic sizing)")
    p.add_argument("--verbose",   action="store_true",
                   help="Enable DEBUG logging")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    # Convert exit time from IST → UTC
    exit_time_utc = _ist_to_utc(args.exit_time)

    # Resolve lot-size: None → dynamic; int → static
    use_dynamic = args.lot_size is None
    lot_size    = args.lot_size if args.lot_size is not None else (
        int(LIVE_LOT_SIZE) if LIVE_LOT_SIZE else 150
    )

    sl_display = "DISABLED (hold to settlement)" if args.sl_pct >= 9999 else f"{args.sl_pct:.0f}%% of entry premium"

    cfg = BacktestConfig(
        # Date range
        start_month     = start,
        end_month       = end,
        # Timing: entry hardcoded at 9AM IST; exit configurable
        entry_time_utc  = ENTRY_TIME_UTC,
        exit_time_utc   = exit_time_utc,
        # OTM configuration
        otm_steps       = args.otm_steps,
        # Risk / capital
        sl_pct          = args.sl_pct,
        initial_capital = args.capital,
        # Lot sizing
        use_dynamic_lot_size = use_dynamic,
        lot_size        = lot_size,
        # Logging
        verbose         = args.verbose,
    )

    otm_distance = args.otm_steps * 200   # USD away from ATM per leg

    log.info("=" * 62)
    log.info("  OTM%d Short Straddle Backtest", args.otm_steps)
    log.info("  Period    : %s to %s", start, end)
    log.info("  Capital   : $%s", f"{args.capital:,.0f}")
    log.info("  Entry     : %s UTC  (09:00 IST)", ENTRY_TIME_UTC)
    log.info("  Exit      : %s UTC  (%s IST)", exit_time_utc, args.exit_time)
    log.info(
        "  Lot size  : %s",
        f"Dynamic ({cfg.capital_allocation_pct:.0f}%% equity, "
        f"{cfg.option_margin_requirement_pct:.0f}%% margin, cap {cfg.max_lot_size:,})"
        if use_dynamic else f"{lot_size} contracts/leg (static)",
    )
    log.info("  SL        : %s", sl_display)
    log.info(
        "  Strikes   : ATM±%d steps  ($%s from ATM per leg)",
        args.otm_steps, f"{otm_distance:,}",
    )
    log.info("=" * 62)

    engine    = ShortStrangleEngine(cfg)
    day_count = 0

    for trade_date, day_df in iter_trading_days(cfg):
        day_count += 1
        engine.run_day(trade_date, day_df)

    log.info("-" * 62)
    log.info(
        "Done: %d calendar days | %d trades executed | %d days skipped",
        day_count, len(engine.trades), len(engine.skipped),
    )

    if not engine.trades:
        log.error("No trades generated — check data path and date range")
        sys.exit(1)

    portfolio = Portfolio(cfg, engine.trades)

    # Attach skipped count for reference
    portfolio.stats["skipped_days"] = len(engine.skipped)

    # ---- Console summary --------------------------------------------------
    _print_summary(portfolio, args.otm_steps, otm_distance, start, end)

    # ---- HTML report ------------------------------------------------------
    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = REPORTS_DIR / f"otm{args.otm_steps}_9am_straddle_{start}_{end}_{ts_str}"
    html    = ReportGenerator(cfg, portfolio).generate(out_dir)

    log.info("Report saved → %s", html)
    print(f"\n  Report: {html}\n")


# ---------------------------------------------------------------------------
# Console summary (mirrors existing backtest format)
# ---------------------------------------------------------------------------

def _print_summary(
    port: Portfolio,
    otm_steps: int,
    otm_distance: int,
    start: str,
    end: str,
) -> None:
    s   = port.stats
    sep = "-" * 62

    sl_label = "DISABLED (settlement)" if port.cfg.sl_pct >= 9999 else f"{port.cfg.sl_pct:.0f}% of premium"
    exit_ist_h = (port.cfg.exit_time_utc.hour * 60 + port.cfg.exit_time_utc.minute + IST_OFFSET_MINUTES) // 60 % 24
    exit_ist_m = (port.cfg.exit_time_utc.hour * 60 + port.cfg.exit_time_utc.minute + IST_OFFSET_MINUTES) % 60

    print()
    print("=" * 62)
    print(f"  OTM{otm_steps} SHORT STRADDLE  |  9:00 AM → {exit_ist_h:02d}:{exit_ist_m:02d} IST")
    print(f"  Call: ATM +{otm_steps} strikes (+${otm_distance:,})  "
          f"Put: ATM -{otm_steps} strikes (-${otm_distance:,})")
    print(f"  SL      : {sl_label}")
    print(f"  Period  : {start} to {end}")
    print(f"  Capital : ${s['initial_capital']:,.0f}")
    print("=" * 62)

    # Trades
    print(f"  Trades       : {s['total_trades']}")
    print(f"  Skipped days : {s.get('skipped_days', '—')}")
    print(f"  Win Rate     : {s['win_rate_pct']:.1f}%  "
          f"({s['win_count']}W / {s['loss_count']}L)")
    print(sep)

    # P&L
    print(f"  Gross P&L      : ${s['gross_pnl_usd']:+,.2f}")
    print(f"  Total Fees     : ${s['total_fee_usd']:+,.2f}")
    print(f"  Slippage       : ${s['total_slippage_usd']:+,.2f}")
    print(f"  Net P&L        : ${s['total_pnl_usd']:+,.2f}  "
          f"({s['total_return_pct']:+.2f}%)")
    print(f"  Final Equity   : ${s['final_equity']:,.2f}")
    print(sep)

    # Per-trade stats
    print(f"  Avg Net/Trade  : ${s['avg_pnl_per_trade']:+.2f}")
    print(f"  Avg Win        : ${s['avg_win_usd']:+.2f}")
    print(f"  Avg Loss       : ${s['avg_loss_usd']:+.2f}")
    print(f"  Best Day       : ${s['max_win_usd']:+.2f}")
    print(f"  Worst Day      : ${s['max_loss_usd']:+.2f}")
    print(f"  Profit Factor  : {s['profit_factor']:.2f}")
    print(sep)

    # Risk metrics
    print(f"  Sharpe Ratio   : {s['sharpe_ratio']:.2f}")
    print(f"  Calmar Ratio   : {s['calmar_ratio']:.2f}")
    print(f"  Max Drawdown   : ${s['max_drawdown_usd']:+,.2f}  "
          f"({s['max_drawdown_pct']:.1f}%)")
    print(sep)

    # Misc
    print(f"  Avg Entry Prem : ${s['avg_entry_premium']:.2f}")
    print(f"  Avg Hold Time  : {s['avg_hold_minutes']:.1f} min")
    print(f"  SL Hits        : {s['sl_hit_count']}")
    print(f"  Time Exits     : {s['time_exit_count']}")
    print("=" * 62)
    print()


if __name__ == "__main__":
    main()
