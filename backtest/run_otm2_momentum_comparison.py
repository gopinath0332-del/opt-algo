"""
backtest/run_otm2_momentum_comparison.py
=========================================
3-way comparison:
  A) Current Config      : 17:00 IST (11:30 UTC), ATM Straddle, No SL, No filter
  B) OTM+2 Strangle      : 13:00 IST (07:30 UTC), OTM+2 Strangle, No SL, No filter
  C) OTM+2 + Momentum    : 13:00 IST (07:30 UTC), OTM+2 Strangle, No SL, Momentum filter

Momentum filter (identical to live config / run_momentum_filter_comparison.py):
  - Estimate BTC spot 2h before entry (05:30 UTC / 11:00 IST) via put-call parity
  - Estimate BTC spot at entry (07:30 UTC / 13:00 IST)
  - If |move| > threshold (default 1.2% from live config) → skip trade

Usage:
  python backtest/run_otm2_momentum_comparison.py
  python backtest/run_otm2_momentum_comparison.py --start 2025-01 --end 2026-06
  python backtest/run_otm2_momentum_comparison.py --threshold 0.8 1.2 1.5 2.0
  python backtest/run_otm2_momentum_comparison.py --month 2025-06
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR, LIVE_CAPITAL_ALLOC_PCT, LIVE_LEVERAGE
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, ShortStrangleEngine, SkippedDay
from backtest.portfolio import Portfolio
from backtest.price_engine import find_atm_strike
from backtest.report import ReportGenerator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time constants  (IST = UTC+5:30)
# ---------------------------------------------------------------------------
# 17:00 IST = 11:30 UTC   — current config entry
# 13:00 IST = 07:30 UTC   — OTM+2 strangle entry
# 11:00 IST = 05:30 UTC   — momentum lookback point (2h before 13:00 IST entry)
# 17:30 IST = 12:00 UTC   — expiry / settlement
ENTRY_1700_UTC   = time(11, 30)   # current config
ENTRY_1300_UTC   = time(7,  30)   # new strangle entry
EXPIRY_EXIT_UTC  = time(12,  0)   # settlement


# ---------------------------------------------------------------------------
# Momentum filter helpers  (reused from run_momentum_filter_comparison.py)
# ---------------------------------------------------------------------------

def estimate_spot_at_time(
    day_df,
    trade_date: date,
    target_time: time,
    window_minutes: int = 10,
) -> Optional[float]:
    """Estimate BTC spot via put-call parity ATM strike."""
    return find_atm_strike(day_df, trade_date, target_time, window_minutes)


def should_skip_momentum(
    day_df,
    trade_date: date,
    entry_time_utc: time,
    lookback_hours: float,
    threshold_pct: float,
    window_minutes: int = 10,
) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """Return (skip, spot_lookback, spot_entry, move_pct)."""
    entry_dt    = datetime.combine(trade_date, entry_time_utc)
    lookback_dt = entry_dt - timedelta(hours=lookback_hours)

    # If lookback crosses midnight, we can't check — conservatively don't skip
    if lookback_dt.date() != trade_date:
        return False, None, None, None

    lookback_time = lookback_dt.time()
    spot_lookback = estimate_spot_at_time(day_df, trade_date, lookback_time, window_minutes)
    spot_entry    = estimate_spot_at_time(day_df, trade_date, entry_time_utc, window_minutes)

    if spot_lookback is None or spot_entry is None or spot_lookback == 0:
        return False, spot_lookback, spot_entry, None

    move_pct = abs(spot_entry - spot_lookback) / spot_lookback * 100.0
    skip     = move_pct > threshold_pct

    return skip, spot_lookback, spot_entry, move_pct


# ---------------------------------------------------------------------------
# Main comparison runner
# ---------------------------------------------------------------------------

def run_all(
    start: str,
    end: str,
    capital: float,
    otm_steps: int,
    threshold_pct: float,
    lookback_hours: float,
) -> dict:
    """
    Load data once, run three engines:
      A  — Current config (ATM straddle, 17:00 IST, no filter)
      B  — OTM+2 strangle (13:00 IST, no filter)
      C  — OTM+2 strangle (13:00 IST, momentum filter)
    """
    base_cfg = dict(
        start_month     = start,
        end_month       = end,
        sl_pct          = 9999.0,        # no stop-loss
        initial_capital = capital,
        exit_time_utc   = EXPIRY_EXIT_UTC,
    )

    cfg_a = BacktestConfig(**base_cfg, entry_time_utc=ENTRY_1700_UTC, otm_steps=0)
    cfg_b = BacktestConfig(**base_cfg, entry_time_utc=ENTRY_1300_UTC, otm_steps=otm_steps)
    cfg_c = BacktestConfig(**base_cfg, entry_time_utc=ENTRY_1300_UTC, otm_steps=otm_steps)

    engine_a = ShortStraddleEngine(cfg_a)
    engine_b = ShortStrangleEngine(cfg_b)
    engine_c = ShortStrangleEngine(cfg_c)

    filter_stats: dict = {
        "threshold_pct":  threshold_pct,
        "lookback_hours": lookback_hours,
        "days_skipped":   0,
        "filter_details": [],
    }

    log.info("Loading data and running all three engines...")
    day_count = 0

    for trade_date, day_df in iter_trading_days(cfg_a):
        day_count += 1

        # A — always runs
        engine_a.run_day(trade_date, day_df)

        # B — always runs (no filter)
        engine_b.run_day(trade_date, day_df)

        # C — momentum-filtered
        skip, spot_lb, spot_entry, move_pct = should_skip_momentum(
            day_df, trade_date,
            entry_time_utc = ENTRY_1300_UTC,
            lookback_hours = lookback_hours,
            threshold_pct  = threshold_pct,
        )
        if skip:
            filter_stats["days_skipped"] += 1
            filter_stats["filter_details"].append({
                "date":          trade_date,
                "spot_lookback": spot_lb,
                "spot_entry":    spot_entry,
                "move_pct":      move_pct,
            })
            engine_c._skip(
                trade_date,
                f"momentum filter: {move_pct:.2f}% > {threshold_pct:.1f}% threshold "
                f"({spot_lb:.0f} → {spot_entry:.0f})"
            )
            log.info(
                "%s | MOMENTUM SKIP | %.2f%% move (%g → %g)",
                trade_date, move_pct, spot_lb or 0, spot_entry or 0,
            )
        else:
            engine_c.run_day(trade_date, day_df)

    log.info(
        "Done: %d days | A=%d trades | B=%d trades | C=%d trades (%d skipped by filter)",
        day_count,
        len(engine_a.trades), len(engine_b.trades), len(engine_c.trades),
        filter_stats["days_skipped"],
    )

    port_a = Portfolio(cfg_a, engine_a.trades)
    port_b = Portfolio(cfg_b, engine_b.trades)
    port_c = Portfolio(cfg_c, engine_c.trades)

    port_a.stats["skipped_days"] = len(engine_a.skipped)
    port_b.stats["skipped_days"] = len(engine_b.skipped)
    port_c.stats["skipped_days"] = len(engine_c.skipped)

    return {
        "engines":      (engine_a, engine_b, engine_c),
        "portfolios":   (port_a, port_b, port_c),
        "configs":      (cfg_a, cfg_b, cfg_c),
        "filter_stats": filter_stats,
        "day_count":    day_count,
        "otm_steps":    otm_steps,
    }


# ---------------------------------------------------------------------------
# Print comparison
# ---------------------------------------------------------------------------

def print_comparison(results: dict) -> None:
    port_a, port_b, port_c = results["portfolios"]
    a = port_a.stats
    b = port_b.stats
    c = port_c.stats
    fs = results["filter_stats"]
    otm_steps = results["otm_steps"]
    otm_dist  = otm_steps * 200

    sep  = "=" * 96
    thin = "-" * 96
    col  = 26

    def row(label: str, av, bv, cv) -> None:
        print(f"  {label:<32}  {str(av):>{col}}  {str(bv):>{col}}  {str(cv):>{col}}")

    print()
    print(sep)
    print("  3-WAY STRATEGY COMPARISON")
    print(sep)
    print(f"  A  :  Current Config  — 17:00 IST (11:30 UTC), ATM Straddle, No SL, No filter")
    print(f"  B  :  OTM+{otm_steps} Strangle   — 13:00 IST (07:30 UTC), ±${otm_dist} OTM, No SL, No filter")
    print(f"  C  :  OTM+{otm_steps} + Momentum  — 13:00 IST (07:30 UTC), ±${otm_dist} OTM, No SL, >{fs['threshold_pct']:.1f}% filter")
    print(sep)
    thr_label = f"C: OTM+2 + Momentum >{fs['threshold_pct']:.1f}%"
    print(f"  {'Metric':<32}  {'A: Current Config':>{col}}  {'B: OTM+2 (no filter)':>{col}}  {thr_label:>{col}}")
    print(thin)

    row("Total Trades",
        a.get("total_trades", "--"),
        b.get("total_trades", "--"),
        c.get("total_trades", "--"))
    row("Days Skipped (filter/data)",
        a.get("skipped_days", 0),
        b.get("skipped_days", 0),
        c.get("skipped_days", 0))
    print(thin)

    row("Win Rate",
        f"{a.get('win_rate_pct', 0):.1f}%",
        f"{b.get('win_rate_pct', 0):.1f}%",
        f"{c.get('win_rate_pct', 0):.1f}%")
    row("Avg Daily P&L",
        f"${a.get('avg_pnl_per_trade', 0):+.2f}",
        f"${b.get('avg_pnl_per_trade', 0):+.2f}",
        f"${c.get('avg_pnl_per_trade', 0):+.2f}")
    row("Total Net P&L",
        f"${a.get('total_pnl_usd', 0):+,.2f}",
        f"${b.get('total_pnl_usd', 0):+,.2f}",
        f"${c.get('total_pnl_usd', 0):+,.2f}")
    row("Gross P&L",
        f"${a.get('gross_pnl_usd', 0):+,.2f}",
        f"${b.get('gross_pnl_usd', 0):+,.2f}",
        f"${c.get('gross_pnl_usd', 0):+,.2f}")
    row("Total Return",
        f"{a.get('total_return_pct', 0):+.1f}%",
        f"{b.get('total_return_pct', 0):+.1f}%",
        f"{c.get('total_return_pct', 0):+.1f}%")
    row("Final Equity",
        f"${a.get('final_equity', 0):,.2f}",
        f"${b.get('final_equity', 0):,.2f}",
        f"${c.get('final_equity', 0):,.2f}")
    print(thin)

    row("Best Day",
        f"${a.get('max_win_usd', 0):+.2f}",
        f"${b.get('max_win_usd', 0):+.2f}",
        f"${c.get('max_win_usd', 0):+.2f}")
    row("Worst Day",
        f"${a.get('max_loss_usd', 0):+.2f}",
        f"${b.get('max_loss_usd', 0):+.2f}",
        f"${c.get('max_loss_usd', 0):+.2f}")
    row("Avg Win",
        f"${a.get('avg_win_usd', 0):+.2f}",
        f"${b.get('avg_win_usd', 0):+.2f}",
        f"${c.get('avg_win_usd', 0):+.2f}")
    row("Avg Loss",
        f"${a.get('avg_loss_usd', 0):+.2f}",
        f"${b.get('avg_loss_usd', 0):+.2f}",
        f"${c.get('avg_loss_usd', 0):+.2f}")
    row("Profit Factor",
        f"{a.get('profit_factor', 0):.2f}",
        f"{b.get('profit_factor', 0):.2f}",
        f"{c.get('profit_factor', 0):.2f}")
    print(thin)

    row("Max Drawdown ($)",
        f"${a.get('max_drawdown_usd', 0):+,.2f}",
        f"${b.get('max_drawdown_usd', 0):+,.2f}",
        f"${c.get('max_drawdown_usd', 0):+,.2f}")
    row("Max Drawdown (%)",
        f"{a.get('max_drawdown_pct', 0):.1f}%",
        f"{b.get('max_drawdown_pct', 0):.1f}%",
        f"{c.get('max_drawdown_pct', 0):.1f}%")
    row("Sharpe Ratio",
        f"{a.get('sharpe_ratio', 0):.2f}",
        f"{b.get('sharpe_ratio', 0):.2f}",
        f"{c.get('sharpe_ratio', 0):.2f}")
    row("Calmar Ratio",
        f"{a.get('calmar_ratio', 0):.2f}",
        f"{b.get('calmar_ratio', 0):.2f}",
        f"{c.get('calmar_ratio', 0):.2f}")
    print(thin)

    row("Avg Entry Premium",
        f"${a.get('avg_entry_premium', 0):.2f}",
        f"${b.get('avg_entry_premium', 0):.2f}",
        f"${c.get('avg_entry_premium', 0):.2f}")
    row("Avg Hold Time",
        f"{a.get('avg_hold_minutes', 0):.0f} min",
        f"{b.get('avg_hold_minutes', 0):.0f} min",
        f"{c.get('avg_hold_minutes', 0):.0f} min")
    row("Total Fees",
        f"${a.get('total_fee_usd', 0):.2f}",
        f"${b.get('total_fee_usd', 0):.2f}",
        f"${c.get('total_fee_usd', 0):.2f}")
    print(sep)

    # Delta: C vs B
    pnl_delta  = c.get("total_pnl_usd", 0) - b.get("total_pnl_usd", 0)
    wr_delta   = c.get("win_rate_pct", 0)  - b.get("win_rate_pct", 0)
    dd_delta   = c.get("max_drawdown_pct", 0) - b.get("max_drawdown_pct", 0)
    sh_delta   = c.get("sharpe_ratio", 0)  - b.get("sharpe_ratio", 0)

    print()
    print(f"  MOMENTUM FILTER IMPACT (C vs B — OTM+2 with vs without filter):")
    print(thin)
    print(f"  Days skipped by filter  : {fs['days_skipped']} / {results['day_count']} "
          f"({fs['days_skipped'] / max(results['day_count'], 1) * 100:.1f}%)")
    print(f"  Net P&L change          : ${pnl_delta:+,.2f}")
    print(f"  Win rate change         : {wr_delta:+.1f}%")
    print(f"  Max Drawdown change     : {dd_delta:+.1f}%")
    print(f"  Sharpe change           : {sh_delta:+.2f}")
    print()

    # Show which days were filtered + their P&L in the unfiltered run
    if fs["filter_details"] and port_b.trade_df is not None and not port_b.trade_df.empty:
        skipped_dates = {d["date"] for d in fs["filter_details"]}
        b_df = port_b.trade_df
        skipped_trades = b_df[b_df["date"].dt.date.isin(skipped_dates)]
        print(f"  OTM+2 P&L on days skipped by momentum filter:")
        if not skipped_trades.empty:
            total_skip_pnl = skipped_trades["net_pnl_usd"].sum()
            avg_skip_pnl   = skipped_trades["net_pnl_usd"].mean()
            wins  = (skipped_trades["net_pnl_usd"] > 0).sum()
            losses= (skipped_trades["net_pnl_usd"] < 0).sum()
            print(f"    Trades matched  : {len(skipped_trades)} (of {len(fs['filter_details'])} skipped days)")
            print(f"    Total P&L       : ${total_skip_pnl:+,.2f}")
            print(f"    Avg P&L/trade   : ${avg_skip_pnl:+,.2f}")
            print(f"    Wins / Losses   : {wins}W / {losses}L")
        print()

    print(sep)

    # Per-day filter detail
    if fs["filter_details"]:
        print(f"\n  Days filtered (>{fs['threshold_pct']:.1f}% move in {fs['lookback_hours']:.0f}h before 13:00 IST entry):")
        print(f"  {'Date':>12}  {'Spot (lookback)':>16}  {'Spot (entry)':>13}  {'Move %':>8}")
        print(f"  {'-'*12}  {'-'*16}  {'-'*13}  {'-'*8}")
        for d in fs["filter_details"]:
            print(
                f"  {str(d['date']):>12}  "
                f"${d['spot_lookback']:>14,.0f}  "
                f"${d['spot_entry']:>11,.0f}  "
                f"{d['move_pct']:>7.2f}%"
            )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="3-way: Current Config vs OTM+2 Strangle vs OTM+2 + Momentum Filter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,      metavar="YYYY-MM",
                   help="Run a single month (overrides --start/--end)")
    p.add_argument("--otm-steps", type=int,   default=2,
                   help="OTM strike steps from ATM (default: 2 = ±$400)")
    p.add_argument("--threshold", type=float, default=1.2,
                   help="Momentum filter threshold %% (default: 1.2, from live config)")
    p.add_argument("--lookback",  type=float, default=2.0,
                   help="Lookback hours before entry (default: 2.0, from live config)")
    p.add_argument("--capital",   type=float, default=1_000.0)
    p.add_argument("--no-report", action="store_true", help="Skip saving HTML reports")
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

    log.info("=" * 60)
    log.info("  OTM+%d Strangle vs Current Config — Momentum Filter Test", args.otm_steps)
    log.info("  Period     : %s → %s", start, end)
    log.info("  Capital    : $%s", f"{args.capital:,.0f}")
    log.info("  OTM steps  : %d (±$%d from ATM)", args.otm_steps, args.otm_steps * 200)
    log.info("  Momentum   : >%.1f%% in %.1fh before entry", args.threshold, args.lookback)
    log.info("=" * 60)

    results = run_all(
        start        = start,
        end          = end,
        capital      = args.capital,
        otm_steps    = args.otm_steps,
        threshold_pct = args.threshold,
        lookback_hours= args.lookback,
    )

    print_comparison(results)

    # Save individual portfolio summaries
    port_a, port_b, port_c = results["portfolios"]
    print("---- A: Current Config (17:00 IST ATM Straddle) ----")
    port_a.print_summary()

    print()
    print(f"---- B: OTM+{args.otm_steps} Strangle (13:00 IST, no filter) ----")
    port_b.print_summary()

    print()
    print(f"---- C: OTM+{args.otm_steps} Strangle (13:00 IST, >{args.threshold:.1f}% filter) ----")
    port_c.print_summary()

    # Save HTML reports
    if not args.no_report:
        from datetime import datetime as _dt
        cfg_a, cfg_b, cfg_c = results["configs"]
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")

        if port_a.trade_df is not None and not port_a.trade_df.empty:
            d = REPORTS_DIR / f"current_config_1700_atm_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_a, port_a).generate(d)
            log.info("A report: %s", h)
            print(f"\n  A (Current Config) report : {h}")

        if port_b.trade_df is not None and not port_b.trade_df.empty:
            d = REPORTS_DIR / f"otm{args.otm_steps}_1300_nofilter_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_b, port_b).generate(d)
            log.info("B report: %s", h)
            print(f"  B (OTM+2 no filter) report : {h}")

        if port_c.trade_df is not None and not port_c.trade_df.empty:
            d = REPORTS_DIR / f"otm{args.otm_steps}_1300_momentum{args.threshold:.1f}pct_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_c, port_c).generate(d)
            log.info("C report: %s", h)
            print(f"  C (OTM+2 + momentum) report : {h}\n")


if __name__ == "__main__":
    main()
