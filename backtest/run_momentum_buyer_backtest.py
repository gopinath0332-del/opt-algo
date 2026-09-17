"""
backtest/run_momentum_buyer_backtest.py
=======================================
Backtest for the BTC Momentum-Triggered Directional Option Buying Strategy.

Strategy logic:
  - Watch window : 16:00 - 17:30 IST (10:30 - 12:00 UTC)
  - Baseline     : ATM strike snapshotted at 16:00 IST
  - Trigger UP   : BTC moves >= +threshold% from baseline -> BUY ATM CE
  - Trigger DOWN : BTC moves <= -threshold% from baseline -> BUY ATM PE
  - Strike       : ATM strike at trigger time
  - Stop-loss    : None (hold to 17:30 IST expiry / settlement)
  - Exit         : 17:30 IST (12:00 UTC) cash settlement

Also runs the standard 17:00 IST straddle as a baseline for comparison.

Usage:
  python backtest/run_momentum_buyer_backtest.py
  python backtest/run_momentum_buyer_backtest.py --month 2025-06
  python backtest/run_momentum_buyer_backtest.py --threshold 0.5 --capital 1000 --alloc-pct 40
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
from backtest.strategy import (
    ShortStraddleEngine,
    MomentumBuyerEngine,
    MomentumBuyerTradeResult,
)
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator

log = logging.getLogger(__name__)

MOMENTUM_ENTRY_UTC = time(10, 30)  # 16:00 IST
STANDARD_ENTRY_UTC = time(11, 30)  # 17:00 IST
EXIT_UTC           = time(12, 0)   # 17:30 IST (expiry)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BTC Directional Momentum Option Buyer Backtest (vs baseline 17:00 straddle)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,       metavar="YYYY-MM",
                   help="Run single month (overrides --start/--end)")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="Momentum trigger threshold %% (default 0.5)")
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
        trades=len(trades), win_rate=win_rate,
        total_pnl=total_pnl, net_return=net_return, avg_pnl=avg_pnl,
        max_dd=max_dd, max_dd_pct=max_dd_pct,
        sharpe=sharpe, calmar=calmar, profit_factor=profit_factor,
        avg_hold_min=avg_hold,
    )


def _buyer_extras(trades: List[MomentumBuyerTradeResult]) -> dict:
    if not trades:
        return {}
    ce_trades = [t for t in trades if t.leg_bought == "CE"]
    pe_trades = [t for t in trades if t.leg_bought == "PE"]

    ce_wins = sum(1 for t in ce_trades if t.net_pnl_usd > 0)
    pe_wins = sum(1 for t in pe_trades if t.net_pnl_usd > 0)

    ce_pnl = sum(t.net_pnl_usd for t in ce_trades)
    pe_pnl = sum(t.net_pnl_usd for t in pe_trades)

    trigger_mins = []
    for t in trades:
        if t.trigger_ts is not None:
            mins = (t.trigger_ts.hour - 10) * 60 + (t.trigger_ts.minute - 30)
            trigger_mins.append(mins)

    return dict(
        ce_trades=len(ce_trades), ce_wins=ce_wins, ce_pnl=ce_pnl,
        pe_trades=len(pe_trades), pe_wins=pe_wins, pe_pnl=pe_pnl,
        avg_trigger_min=float(np.mean(trigger_mins)) if trigger_mins else 0.0,
        avg_move_pct=float(np.mean([abs(t.trigger_move_pct) for t in trades])),
    )


def _print_comparison(buy_m, base_m, buyer_extra, total_days, triggered_days):
    W = 76
    print("=" * W)
    print(f"{'METRIC':<34} {'MOMENTUM BUYER (CE/PE)':>20} {'BASELINE 17:00 STRADDLE':>20}")
    print("-" * W)

    def row(label, key, fmt=".2f", suffix=""):
        bv = buy_m.get(key, 0)
        sv = base_m.get(key, 0)
        print(f"  {label:<32} {f'{bv:{fmt}}{suffix}':>20} {f'{sv:{fmt}}{suffix}':>20}")

    row("Total Trades",     "trades",        ".0f")
    row("Win Rate",         "win_rate",      ".1f", "%")
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
    print(f"\n  {'Total trading days':<32} {total_days:>20}")
    print(f"  {'Momentum triggered days':<32} {triggered_days:>20}")
    print(f"  {'Trigger rate':<32} {trig_rate:>19.1f}%")
    if buyer_extra:
        amin = buyer_extra.get("avg_trigger_min", 0)
        # mins after 10:30 UTC. In IST, 10:30 UTC = 16:00 IST.
        # So minutes after 16:00 IST = amin.
        ist_total_mins = 16 * 60 + int(amin)
        ah = ist_total_mins // 60
        am = ist_total_mins % 60
        print(f"  {'Avg trigger time (IST)':<32} {f'{ah:02d}:{am:02d} IST':>20}")
        print(f"  {'Avg trigger move':<32} {buyer_extra['avg_move_pct']:>19.3f}%")
        ce_cnt = buyer_extra["ce_trades"]
        ce_wr  = (buyer_extra["ce_wins"] / ce_cnt * 100) if ce_cnt else 0.0
        ce_pnl = buyer_extra["ce_pnl"]
        pe_cnt = buyer_extra["pe_trades"]
        pe_wr  = (buyer_extra["pe_wins"] / pe_cnt * 100) if pe_cnt else 0.0
        pe_pnl = buyer_extra["pe_pnl"]
        print(f"  {'CE Trades (Buy on UP)':<32} {f'{ce_cnt} (WR {ce_wr:.1f}%, ${ce_pnl:,.2f})':>20}")
        print(f"  {'PE Trades (Buy on DOWN)':<32} {f'{pe_cnt} (WR {pe_wr:.1f}%, ${pe_pnl:,.2f})':>20}")
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

    log.info("=" * 76)
    log.info("  BTC Momentum Option Buyer Backtest (Long CE / Long PE)")
    log.info("  Period    : %s -> %s", start, end)
    log.info("  Capital   : $%s | Alloc: %.0f%%", f"{args.capital:,.0f}", args.alloc_pct)
    log.info("  Threshold : %+.2f%% UP -> BUY CE  |  %-.2f%% DOWN -> BUY PE", args.threshold, -args.threshold)
    log.info("  Window    : 16:00 -> 17:30 IST (Hold to Expiry)")
    log.info("=" * 76)

    buy_cfg = BacktestConfig(
        start_month=start, end_month=end,
        entry_time_utc=MOMENTUM_ENTRY_UTC,
        exit_time_utc=EXIT_UTC,
        sl_pct=9999.0,
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

    buy_engine  = MomentumBuyerEngine(buy_cfg)
    base_engine = ShortStraddleEngine(base_cfg)

    total_days = 0
    triggered_days = 0

    log.info("\n--- Directional Momentum Buyer Strategy (16:00-17:30 IST, %.2f%% trigger) ---", args.threshold)
    for trade_date, day_df in iter_trading_days(buy_cfg):
        total_days += 1
        result = buy_engine.run_day(trade_date, day_df)
        if result is not None:
            triggered_days += 1

    log.info("\n--- Baseline Strategy (17:00 IST short straddle, no SL) ---")
    for trade_date, day_df in iter_trading_days(base_cfg):
        base_engine.run_day(trade_date, day_df)

    log.info("-" * 76)
    no_trigger_count = sum(1 for s in buy_engine.skipped if "momentum trigger" in s.reason)
    log.info(
        "Buyer: %d days | %d trades | %d skipped (%d no-trigger)",
        total_days, len(buy_engine.trades), len(buy_engine.skipped), no_trigger_count,
    )
    log.info("Baseline: %d trades | %d skipped", len(base_engine.trades), len(base_engine.skipped))

    if not buy_engine.trades and not base_engine.trades:
        log.error("No trades generated -- check data path and date range")
        sys.exit(1)

    buy_m    = _metrics(buy_engine.trades,  args.capital)
    base_m   = _metrics(base_engine.trades, args.capital)
    buyer_extra = _buyer_extras(buy_engine.trades)

    _print_comparison(buy_m, base_m, buyer_extra, total_days, triggered_days)

    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = REPORTS_DIR / f"momentum_buyer_{start}_{end}_{ts_str}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if buy_engine.trades:
        buy_portfolio = Portfolio(buy_cfg, buy_engine.trades)
        buy_portfolio.print_summary()
        gen  = ReportGenerator(buy_cfg, buy_portfolio)
        html = gen.generate(out_dir / "buyer")
        print(f"\n  Open momentum buyer report: {html}")

    if base_engine.trades:
        base_portfolio = Portfolio(base_cfg, base_engine.trades)
        gen  = ReportGenerator(base_cfg, base_portfolio)
        html = gen.generate(out_dir / "baseline")
        print(f"  Open baseline report      : {html}")


if __name__ == "__main__":
    main()
