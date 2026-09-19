"""
backtest/run_fixed_lot_comparison.py
====================================
Comprehensive comparison of Dynamic Lot Size vs Fixed Lot Size (200 contracts).

Compares:
  1. Dynamic Lot Sizing (Current Config: 40% capital allocation, 200x leverage, capped at 1000 lots)
  2. Fixed Lot Sizing (200 lots static per leg)

Both variants are tested with:
  - Active Settings: 17:00 IST entry (11:30 UTC), 17:30 IST settlement (12:00 UTC),
    Momentum Filter (1.2% threshold, 2h lookback), and Big-Leg Skip Filter (10x ratio).
  - Also displays raw (all-day) comparison for complete transparency.

Usage:
  python backtest/run_fixed_lot_comparison.py
  python backtest/run_fixed_lot_comparison.py --start 2025-01 --end 2026-06
  python backtest/run_fixed_lot_comparison.py --start 2025-01 --end 2026-09
  python backtest/run_fixed_lot_comparison.py --fixed-lots 200
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import (
    BacktestConfig,
    REPORTS_DIR,
    LIVE_ENTRY_TIME,
    LIVE_EXIT_TIME,
    LIVE_CAPITAL_ALLOC_PCT,
    LIVE_LEVERAGE,
)
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, TradeResult
from backtest.portfolio import Portfolio
from backtest.price_engine import find_atm_strike, get_straddle_price
from backtest.report import ReportGenerator

log = logging.getLogger(__name__)


def estimate_spot_at_time(
    day_df: pd.DataFrame,
    trade_date: date,
    target_time: time,
    window_minutes: int = 10,
) -> Optional[float]:
    """Estimate BTC spot price at target time using ATM strike."""
    return find_atm_strike(day_df, trade_date, target_time, window_minutes)


def should_skip_momentum(
    day_df: pd.DataFrame,
    trade_date: date,
    entry_time_utc: time,
    lookback_hours: float,
    threshold_pct: float,
    window_minutes: int = 10,
) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """Check if BTC move over lookback window exceeds threshold."""
    entry_dt = datetime.combine(trade_date, entry_time_utc)
    lookback_dt = entry_dt - timedelta(hours=lookback_hours)

    if lookback_dt.date() != trade_date:
        return False, None, None, None

    lookback_time = lookback_dt.time()
    spot_lb = estimate_spot_at_time(day_df, trade_date, lookback_time, window_minutes)
    spot_en = estimate_spot_at_time(day_df, trade_date, entry_time_utc, window_minutes)

    if spot_lb is None or spot_en is None or spot_lb <= 0:
        return False, spot_lb, spot_en, None

    move_pct = abs(spot_en - spot_lb) / spot_lb * 100.0
    return move_pct > threshold_pct, spot_lb, spot_en, move_pct


def should_skip_big_leg(
    day_df: pd.DataFrame,
    atm: float,
    trade_date: date,
    entry_time_utc: time,
    window_minutes: int = 5,
    skip_ratio: float = 10.0,
) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """Check if call/put mark price ratio >= skip_ratio."""
    if skip_ratio <= 0:
        return False, None, None, None

    call_p, put_p = get_straddle_price(day_df, atm, trade_date, entry_time_utc, window_minutes)
    if call_p is None or put_p is None or call_p <= 0 or put_p <= 0:
        return False, call_p, put_p, None

    big = max(call_p, put_p)
    small = min(call_p, put_p)
    ratio = big / small if small > 0 else float("inf")
    return ratio >= skip_ratio, call_p, put_p, ratio


def compute_extended_stats(portfolio: Portfolio) -> Dict[str, Any]:
    """Compute detailed analytics beyond standard portfolio stats."""
    stats = dict(portfolio.stats)
    df = portfolio.trade_df

    if df is None or df.empty:
        return stats

    pnl = df["net_pnl_usd"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    stats["win_count"] = len(wins)
    stats["loss_count"] = len(losses)
    stats["avg_trade_usd"] = float(pnl.mean())
    stats["avg_win_usd"] = float(wins.mean()) if len(wins) > 0 else 0.0
    stats["avg_loss_usd"] = float(losses.mean()) if len(losses) > 0 else 0.0
    stats["win_loss_ratio"] = abs(stats["avg_win_usd"] / stats["avg_loss_usd"]) if stats["avg_loss_usd"] != 0 else 0.0

    # Sortino Ratio (downside risk only, annualised)
    returns = df["return_pct"] / 100.0
    neg_returns = returns[returns < 0]
    downside_dev = np.sqrt((neg_returns ** 2).mean()) if len(neg_returns) > 0 else 0.0
    stats["sortino_ratio"] = (returns.mean() / downside_dev * np.sqrt(252)) if downside_dev > 0 else 0.0

    # Winning / Losing streaks
    streaks = []
    current_streak = 0
    current_sign = 0
    for val in pnl:
        sign = 1 if val > 0 else (-1 if val < 0 else 0)
        if sign == current_sign:
            current_streak += sign
        else:
            streaks.append(current_streak)
            current_sign = sign
            current_streak = sign
    streaks.append(current_streak)

    stats["max_win_streak"] = max([s for s in streaks if s > 0], default=0)
    stats["max_loss_streak"] = abs(min([s for s in streaks if s < 0], default=0))

    # Lot size statistics
    stats["min_lot_size"] = int(df["lot_size"].min())
    stats["max_lot_size"] = int(df["lot_size"].max())
    stats["avg_lot_size"] = float(df["lot_size"].mean())

    return stats


def run_comparison(
    start: str = "2025-01",
    end: str = "2026-06",
    fixed_lots: int = 200,
    capital: float = 1000.0,
    use_filters: bool = True,
    momentum_threshold: float = 1.2,
    momentum_lookback: float = 2.0,
    big_leg_skip: float = 10.0,
    skip_weekends: bool = False,
    verbose: bool = False,
) -> Tuple[Portfolio, Portfolio, Dict[str, Any]]:
    """Run Dynamic Lot Size vs Fixed Lot Size engines on all days."""
    
    # 1. Dynamic Lot Config
    cfg_dynamic = BacktestConfig(
        start_month=start,
        end_month=end,
        entry_time_utc=LIVE_ENTRY_TIME,
        exit_time_utc=LIVE_EXIT_TIME,
        initial_capital=capital,
        use_dynamic_lot_size=True,
        capital_allocation_pct=LIVE_CAPITAL_ALLOC_PCT,
        leverage=LIVE_LEVERAGE,
        max_lot_size=1000,
        sl_pct=9999.0,
        verbose=verbose,
    )

    # 2. Fixed Lot Config
    cfg_fixed = BacktestConfig(
        start_month=start,
        end_month=end,
        entry_time_utc=LIVE_ENTRY_TIME,
        exit_time_utc=LIVE_EXIT_TIME,
        initial_capital=capital,
        use_dynamic_lot_size=False,
        lot_size=fixed_lots,
        sl_pct=9999.0,
        verbose=verbose,
    )

    eng_dynamic = ShortStraddleEngine(cfg_dynamic)
    eng_fixed = ShortStraddleEngine(cfg_fixed)

    total_days = 0
    mom_skipped = 0
    big_leg_skipped = 0
    weekend_skipped = 0

    log.info("Loading data and processing days for %s -> %s...", start, end)

    for trade_date, day_df in iter_trading_days(cfg_dynamic):
        total_days += 1

        if skip_weekends and trade_date.weekday() in (5, 6):
            eng_dynamic._skip(trade_date, "weekend trade (Saturday/Sunday)")
            eng_fixed._skip(trade_date, "weekend trade (Saturday/Sunday)")
            weekend_skipped += 1
            continue

        if use_filters:
            # Check momentum filter
            skip_mom, spot_lb, spot_en, move = should_skip_momentum(
                day_df, trade_date, LIVE_ENTRY_TIME,
                momentum_lookback, momentum_threshold,
            )
            if skip_mom:
                reason = f"momentum filter: {move:.2f}% move in {momentum_lookback:.0f}h"
                eng_dynamic._skip(trade_date, reason)
                eng_fixed._skip(trade_date, reason)
                mom_skipped += 1
                continue

            # Check ATM strike & big-leg ratio filter
            atm = find_atm_strike(day_df, trade_date, LIVE_ENTRY_TIME, 5)
            if atm is not None and big_leg_skip > 0:
                skip_bl, cp, pp, ratio = should_skip_big_leg(
                    day_df, atm, trade_date, LIVE_ENTRY_TIME, 5, big_leg_skip
                )
                if skip_bl:
                    reason = f"big-leg skip filter: ratio {ratio:.1f}x >= {big_leg_skip:.0f}x"
                    eng_dynamic._skip(trade_date, reason)
                    eng_fixed._skip(trade_date, reason)
                    big_leg_skipped += 1
                    continue

        eng_dynamic.run_day(trade_date, day_df)
        eng_fixed.run_day(trade_date, day_df)

    port_dynamic = Portfolio(cfg_dynamic, eng_dynamic.trades)
    port_fixed = Portfolio(cfg_fixed, eng_fixed.trades)

    meta = {
        "total_days": total_days,
        "mom_skipped": mom_skipped,
        "big_leg_skipped": big_leg_skipped,
        "weekend_skipped": weekend_skipped,
        "traded_days": len(eng_fixed.trades),
    }

    return port_dynamic, port_fixed, meta


def print_comparison_tables(
    port_dyn: Portfolio,
    port_fix: Portfolio,
    meta: Dict[str, Any],
    fixed_lots: int,
    period: str,
    filter_label: str,
) -> None:
    """Print beautifully formatted comparison tables and monthly breakdowns."""
    s_dyn = compute_extended_stats(port_dyn)
    s_fix = compute_extended_stats(port_fix)

    w = 68
    print("\n" + "=" * w)
    print(f"  BACKTEST COMPARISON: DYNAMIC LOT SIZE vs FIXED {fixed_lots} LOTS")
    print(f"  Period: {period} | Filters: {filter_label}")
    print("=" * w)
    print(f"  Calendar Days     : {meta['total_days']}")
    print(f"  Trades Executed   : {meta['traded_days']}")
    print(f"  Momentum Skips    : {meta['mom_skipped']}")
    print(f"  Big-Leg Skips     : {meta['big_leg_skipped']}")
    print("-" * w)

    col1 = 28
    col2 = 18
    col3 = 18

    def p_row(label: str, v1: str, v2: str):
        print(f"  {label:<{col1}} {v1:>{col2}} {v2:>{col3}}")

    print(f"  {'Metric':<{col1}} {'Dynamic Lot Sizing':>{col2}} {f'Fixed {fixed_lots} Lots':>{col3}}")
    print("  " + "-" * (col1 + col2 + col3 + 2))

    p_row("Lot Size Strategy", "Dynamic (40% alloc)", f"Fixed ({fixed_lots} lots)")
    p_row("Average Lot Size", f"{s_dyn.get('avg_lot_size', 0):.0f} (max 1000)", f"{fixed_lots}")
    p_row("Lot Size Range", f"{s_dyn.get('min_lot_size', 0)} - {s_dyn.get('max_lot_size', 0)}", f"{fixed_lots}")
    p_row("Initial Capital", f"${port_dyn.cfg.initial_capital:,.2f}", f"${port_fix.cfg.initial_capital:,.2f}")
    p_row("Final Equity", f"${s_dyn['final_equity']:,.2f}", f"${s_fix['final_equity']:,.2f}")
    p_row("Total Net P&L", f"${s_dyn['total_pnl_usd']:+,.2f}", f"${s_fix['total_pnl_usd']:+,.2f}")
    p_row("Total Return", f"{s_dyn['total_return_pct']:+,.1f}%", f"{s_fix['total_return_pct']:+,.1f}%")
    p_row("Win Rate", f"{s_dyn['win_rate_pct']:.1f}% ({s_dyn['win_count']}W/{s_dyn['loss_count']}L)",
          f"{s_fix['win_rate_pct']:.1f}% ({s_fix['win_count']}W/{s_fix['loss_count']}L)")
    p_row("Profit Factor", f"{s_dyn['profit_factor']:.2f}", f"{s_fix['profit_factor']:.2f}")
    p_row("Sharpe Ratio", f"{s_dyn['sharpe_ratio']:.2f}", f"{s_fix['sharpe_ratio']:.2f}")
    p_row("Sortino Ratio", f"{s_dyn.get('sortino_ratio', 0):.2f}", f"{s_fix.get('sortino_ratio', 0):.2f}")
    p_row("Calmar Ratio", f"{s_dyn['calmar_ratio']:.2f}", f"{s_fix['calmar_ratio']:.2f}")
    p_row("Max Drawdown ($)", f"${s_dyn['max_drawdown_usd']:+,.2f}", f"${s_fix['max_drawdown_usd']:+,.2f}")
    p_row("Max Drawdown (%)", f"{s_dyn['max_drawdown_pct']:.1f}%", f"{s_fix['max_drawdown_pct']:.1f}%")
    p_row("Avg Trade P&L", f"${s_dyn['avg_trade_usd']:+,.2f}", f"${s_fix['avg_trade_usd']:+,.2f}")
    p_row("Avg Win", f"${s_dyn['avg_win_usd']:+,.2f}", f"${s_fix['avg_win_usd']:+,.2f}")
    p_row("Avg Loss", f"${s_dyn['avg_loss_usd']:+,.2f}", f"${s_fix['avg_loss_usd']:+,.2f}")
    p_row("Win / Loss Ratio", f"{s_dyn['win_loss_ratio']:.2f}", f"{s_fix['win_loss_ratio']:.2f}")
    p_row("Max Win Streak", f"{s_dyn['max_win_streak']} trades", f"{s_fix['max_win_streak']} trades")
    p_row("Max Loss Streak", f"{s_dyn['max_loss_streak']} trades", f"{s_fix['max_loss_streak']} trades")
    p_row("Total Fees Paid", f"${s_dyn['total_fee_usd']:,.2f}", f"${s_fix['total_fee_usd']:,.2f}")
    print("=" * w)

    # Monthly breakdown
    print("\n  MONTH-BY-MONTH NET P&L COMPARISON:")
    print("  " + "-" * 64)
    print(f"  {'Month':<10} {'Dynamic PnL':>14} {'Fixed PnL':>14} {'Dyn Ret%':>12} {'Fix Ret%':>12}")
    print("  " + "-" * 64)

    df_d = port_dyn.trade_df.copy()
    df_f = port_fix.trade_df.copy()
    df_d["month"] = df_d["date"].dt.strftime("%Y-%m")
    df_f["month"] = df_f["date"].dt.strftime("%Y-%m")

    m_d = df_d.groupby("month")["net_pnl_usd"].sum()
    m_f = df_f.groupby("month")["net_pnl_usd"].sum()
    all_months = sorted(list(set(m_d.index).union(set(m_f.index))))

    for m in all_months:
        vd = m_d.get(m, 0.0)
        vf = m_f.get(m, 0.0)
        rd = (vd / port_dyn.cfg.initial_capital) * 100.0
        rf = (vf / port_fix.cfg.initial_capital) * 100.0
        print(f"  {m:<10} {vd:>+14,.2f} {vf:>+14,.2f} {rd:>+11.1f}% {rf:>+11.1f}%")

    print("  " + "-" * 64)
    print(f"  {'TOTAL':<10} {s_dyn['total_pnl_usd']:>+14,.2f} {s_fix['total_pnl_usd']:>+14,.2f} "
          f"{s_dyn['total_return_pct']:>+11.1f}% {s_fix['total_return_pct']:>+11.1f}%")
    print("  " + "=" * 64 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Compare Dynamic Lot Size vs Fixed 200 Lots")
    parser.add_argument("--start", default="2025-01", help="Start month YYYY-MM")
    parser.add_argument("--end", default="2026-06", help="End month YYYY-MM")
    parser.add_argument("--fixed-lots", type=int, default=200, help="Fixed lot size to test")
    parser.add_argument("--capital", type=float, default=1000.0, help="Initial capital USD")
    parser.add_argument("--all-days", action="store_true", help="Run without filters (all days)")
    parser.add_argument("--save-reports", action="store_true", help="Generate HTML reports")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    # 1. Run with active filters (Momentum Filter 1.2% + Big-Leg Skip 10x)
    print("\n>>> Running Backtest with ACTIVE FILTERS (Momentum 1.2% + Big Leg 10x)...")
    port_dyn_f, port_fix_f, meta_f = run_comparison(
        start=args.start,
        end=args.end,
        fixed_lots=args.fixed_lots,
        capital=args.capital,
        use_filters=True,
        verbose=args.verbose,
    )

    print_comparison_tables(
        port_dyn_f,
        port_fix_f,
        meta_f,
        fixed_lots=args.fixed_lots,
        period=f"{args.start} -> {args.end}",
        filter_label="Momentum 1.2% + Big-Leg 10x (Live Settings)",
    )

    # 2. Run Raw Strategy (All days, no filters)
    if args.all_days:
        print("\n>>> Running Backtest RAW (All days, No Filters)...")
        port_dyn_raw, port_fix_raw, meta_raw = run_comparison(
            start=args.start,
            end=args.end,
            fixed_lots=args.fixed_lots,
            capital=args.capital,
            use_filters=False,
            verbose=args.verbose,
        )
        print_comparison_tables(
            port_dyn_raw,
            port_fix_raw,
            meta_raw,
            fixed_lots=args.fixed_lots,
            period=f"{args.start} -> {args.end}",
            filter_label="Raw (All Days, No Filters)",
        )

    if args.save_reports:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        d_dyn = REPORTS_DIR / f"dynamic_lots_{args.start}_{args.end}_{ts}"
        d_fix = REPORTS_DIR / f"fixed_{args.fixed_lots}lots_{args.start}_{args.end}_{ts}"
        h_dyn = ReportGenerator(port_dyn_f.cfg, port_dyn_f).generate(d_dyn)
        h_fix = ReportGenerator(port_fix_f.cfg, port_fix_f).generate(d_fix)
        print(f"Dynamic report: {h_dyn}")
        print(f"Fixed {args.fixed_lots} report: {h_fix}")


if __name__ == "__main__":
    main()
