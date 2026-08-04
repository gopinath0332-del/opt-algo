"""
backtest/run_weekend_comparison.py
===================================
Compare backtest results: skip_weekends=True vs skip_weekends=False.

Runs both variants on the full date range, computes detailed metrics,
and prints a side-by-side comparison plus weekend-specific analysis.
"""

from __future__ import annotations

import logging
import sys
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow running from the opt-algo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine
from backtest.portfolio import Portfolio


def run_backtest(skip_weekends: bool) -> tuple[Portfolio, ShortStraddleEngine]:
    """Run the backtest with or without skipping weekends."""
    cfg = BacktestConfig(
        start_month="2025-01",
        end_month="2026-06",
        verbose=False,
    )

    engine = ShortStraddleEngine(cfg)

    for trade_date, day_df in iter_trading_days(cfg):
        if skip_weekends and trade_date.weekday() in (5, 6):
            engine._skip(trade_date, "weekend trade (Saturday/Sunday)")
            continue
        engine.run_day(trade_date, day_df)

    portfolio = Portfolio(cfg, engine.trades)
    return portfolio, engine


def extract_weekend_trades(portfolio: Portfolio) -> pd.DataFrame:
    """Get only Saturday/Sunday trades from a portfolio."""
    df = portfolio.trade_df.copy()
    df["dow_num"] = pd.to_datetime(df["date"]).dt.weekday
    return df[df["dow_num"].isin([5, 6])]


def format_val(val, fmt=",.2f"):
    """Format a value safely."""
    if isinstance(val, float):
        if np.isinf(val):
            return "∞"
        return f"{val:{fmt}}"
    return str(val)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    log = logging.getLogger(__name__)

    # ======================================================================
    # Run both backtests
    # ======================================================================
    log.info("=" * 70)
    log.info("  RUNNING BACKTEST: skip_weekends = False (ALL days)")
    log.info("=" * 70)
    port_all, engine_all = run_backtest(skip_weekends=False)

    log.info("")
    log.info("=" * 70)
    log.info("  RUNNING BACKTEST: skip_weekends = True (weekdays only)")
    log.info("=" * 70)
    port_skip, engine_skip = run_backtest(skip_weekends=True)

    s_all  = port_all.stats
    s_skip = port_skip.stats
    df_all  = port_all.trade_df
    df_skip = port_skip.trade_df

    # ======================================================================
    # Weekend-only trades analysis
    # ======================================================================
    weekend_df = extract_weekend_trades(port_all)
    n_weekend = len(weekend_df)
    weekend_net_pnl = float(weekend_df["net_pnl_usd"].sum()) if n_weekend > 0 else 0.0
    weekend_avg_pnl = float(weekend_df["net_pnl_usd"].mean()) if n_weekend > 0 else 0.0
    weekend_win_count = int((weekend_df["net_pnl_usd"] > 0).sum()) if n_weekend > 0 else 0
    weekend_loss_count = int((weekend_df["net_pnl_usd"] < 0).sum()) if n_weekend > 0 else 0
    weekend_win_rate = weekend_win_count / n_weekend * 100 if n_weekend > 0 else 0.0
    weekend_sl_hits = int((weekend_df["exit_reason"] == "sl_hit").sum()) if n_weekend > 0 else 0

    # Weekend day-of-week breakdown
    sat_df = weekend_df[weekend_df["dow_num"] == 5] if n_weekend > 0 else pd.DataFrame()
    sun_df = weekend_df[weekend_df["dow_num"] == 6] if n_weekend > 0 else pd.DataFrame()

    # Weekday-only trades from all-days backtest
    weekday_df = df_all[~pd.to_datetime(df_all["date"]).dt.weekday.isin([5, 6])]
    n_weekday = len(weekday_df)
    weekday_net_pnl = float(weekday_df["net_pnl_usd"].sum()) if n_weekday > 0 else 0.0
    weekday_avg_pnl = float(weekday_df["net_pnl_usd"].mean()) if n_weekday > 0 else 0.0

    # ======================================================================
    # Compute additional metrics for both
    # ======================================================================

    def compute_extra_metrics(df: pd.DataFrame, stats: dict, initial_capital: float) -> dict:
        """Compute extra risk metrics not in the standard stats."""
        pnl = df["net_pnl_usd"]
        equity = initial_capital + pnl.cumsum()

        # Sortino ratio (annualised, downside deviation)
        daily_ret = pnl / initial_capital
        downside = daily_ret[daily_ret < 0]
        downside_std = downside.std() if len(downside) > 1 else 0
        sortino = (daily_ret.mean() / downside_std * np.sqrt(252)) if downside_std > 0 else 0.0

        # Average daily return
        avg_daily_ret_pct = float(daily_ret.mean() * 100)

        # Std of daily returns
        std_daily_ret_pct = float(daily_ret.std() * 100)

        # Max consecutive wins/losses
        signs = (pnl > 0).astype(int)
        max_consec_wins = 0
        max_consec_losses = 0
        cur_wins = 0
        cur_losses = 0
        for v in signs:
            if v == 1:
                cur_wins += 1
                cur_losses = 0
            else:
                cur_losses += 1
                cur_wins = 0
            max_consec_wins = max(max_consec_wins, cur_wins)
            max_consec_losses = max(max_consec_losses, cur_losses)

        # Recovery factor
        max_dd = abs(stats["max_drawdown_usd"]) if stats["max_drawdown_usd"] != 0 else 1.0
        recovery_factor = float(pnl.sum()) / max_dd if max_dd > 0 else 0.0

        # Expectancy per trade
        win_rate = stats["win_rate_pct"] / 100.0
        avg_win = stats["avg_win_usd"] if stats["avg_win_usd"] > 0 else 0.0
        avg_loss = abs(stats["avg_loss_usd"]) if stats["avg_loss_usd"] < 0 else 0.0
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

        # Payoff ratio (avg win / avg loss)
        payoff_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # Kelly criterion
        kelly = (win_rate - (1 - win_rate) / payoff_ratio) if payoff_ratio > 0 and not np.isinf(payoff_ratio) else 0.0

        return {
            "sortino_ratio": sortino,
            "avg_daily_ret_pct": avg_daily_ret_pct,
            "std_daily_ret_pct": std_daily_ret_pct,
            "max_consec_wins": max_consec_wins,
            "max_consec_losses": max_consec_losses,
            "recovery_factor": recovery_factor,
            "expectancy_usd": expectancy,
            "payoff_ratio": payoff_ratio,
            "kelly_criterion": kelly,
        }

    extra_all  = compute_extra_metrics(df_all,  s_all,  1000.0)
    extra_skip = compute_extra_metrics(df_skip, s_skip, 1000.0)

    # ======================================================================
    # Print comparison
    # ======================================================================
    sep = "=" * 90
    thin_sep = "-" * 90

    print(f"\n{sep}")
    print("   BTC SHORT STRADDLE BACKTEST: WEEKEND COMPARISON")
    print(f"   Period: {s_all.get('total_trades', 0)} vs {s_skip.get('total_trades', 0)} trades")
    print(f"   Data range: 2025-01 to 2026-06")
    print(sep)

    header = f"{'Metric':<40} {'All Days':>20} {'Skip Weekends':>20} {'Delta':>10}"
    print(f"\n{header}")
    print(thin_sep)

    def print_row(label, val_all, val_skip, fmt=",.2f", prefix="$", suffix="", invert_color=False):
        s1 = f"{prefix}{val_all:{fmt}}{suffix}" if isinstance(val_all, (int, float)) and not np.isinf(val_all) else str(val_all)
        s2 = f"{prefix}{val_skip:{fmt}}{suffix}" if isinstance(val_skip, (int, float)) and not np.isinf(val_skip) else str(val_skip)
        if isinstance(val_all, (int, float)) and isinstance(val_skip, (int, float)):
            delta = val_skip - val_all
            d = f"{prefix}{delta:+{fmt}}{suffix}" if not np.isinf(delta) else "∞"
        else:
            d = "—"
        print(f"  {label:<38} {s1:>20} {s2:>20} {d:>10}")

    # --- Core P&L ---
    print(f"\n  {'── PROFITABILITY ──'}")
    print_row("Total Net P&L", s_all["total_pnl_usd"], s_skip["total_pnl_usd"])
    print_row("Total Gross P&L", s_all["gross_pnl_usd"], s_skip["gross_pnl_usd"])
    print_row("Total Return %", s_all["total_return_pct"], s_skip["total_return_pct"], fmt=".2f", prefix="", suffix="%")
    print_row("Final Equity", s_all["final_equity"], s_skip["final_equity"])
    print_row("Avg Net P&L / Trade", s_all["avg_pnl_per_trade"], s_skip["avg_pnl_per_trade"])
    print_row("Expectancy / Trade", extra_all["expectancy_usd"], extra_skip["expectancy_usd"])

    # --- Win/Loss ---
    print(f"\n  {'── WIN/LOSS PROFILE ──'}")
    print_row("Total Trades", s_all["total_trades"], s_skip["total_trades"], fmt="d", prefix="")
    print_row("Wins", s_all["win_count"], s_skip["win_count"], fmt="d", prefix="")
    print_row("Losses", s_all["loss_count"], s_skip["loss_count"], fmt="d", prefix="")
    print_row("Win Rate %", s_all["win_rate_pct"], s_skip["win_rate_pct"], fmt=".1f", prefix="", suffix="%")
    print_row("Avg Win", s_all["avg_win_usd"], s_skip["avg_win_usd"])
    print_row("Avg Loss", s_all["avg_loss_usd"], s_skip["avg_loss_usd"])
    print_row("Max Win", s_all["max_win_usd"], s_skip["max_win_usd"])
    print_row("Max Loss", s_all["max_loss_usd"], s_skip["max_loss_usd"])
    print_row("Profit Factor", s_all["profit_factor"], s_skip["profit_factor"], fmt=".2f", prefix="")
    print_row("Payoff Ratio", extra_all["payoff_ratio"], extra_skip["payoff_ratio"], fmt=".2f", prefix="")
    print_row("Max Consec Wins", extra_all["max_consec_wins"], extra_skip["max_consec_wins"], fmt="d", prefix="")
    print_row("Max Consec Losses", extra_all["max_consec_losses"], extra_skip["max_consec_losses"], fmt="d", prefix="")

    # --- Risk ---
    print(f"\n  {'── RISK METRICS ──'}")
    print_row("Max Drawdown $", s_all["max_drawdown_usd"], s_skip["max_drawdown_usd"])
    print_row("Max Drawdown %", s_all["max_drawdown_pct"], s_skip["max_drawdown_pct"], fmt=".1f", prefix="", suffix="%")
    print_row("Sharpe Ratio", s_all["sharpe_ratio"], s_skip["sharpe_ratio"], fmt=".2f", prefix="")
    print_row("Sortino Ratio", extra_all["sortino_ratio"], extra_skip["sortino_ratio"], fmt=".2f", prefix="")
    print_row("Calmar Ratio", s_all["calmar_ratio"], s_skip["calmar_ratio"], fmt=".2f", prefix="")
    print_row("Recovery Factor", extra_all["recovery_factor"], extra_skip["recovery_factor"], fmt=".2f", prefix="")
    print_row("Avg Daily Return %", extra_all["avg_daily_ret_pct"], extra_skip["avg_daily_ret_pct"], fmt=".4f", prefix="", suffix="%")
    print_row("Std Daily Return %", extra_all["std_daily_ret_pct"], extra_skip["std_daily_ret_pct"], fmt=".4f", prefix="", suffix="%")
    print_row("Kelly Criterion", extra_all["kelly_criterion"], extra_skip["kelly_criterion"], fmt=".4f", prefix="")

    # --- Costs ---
    print(f"\n  {'── COSTS & EFFICIENCY ──'}")
    print_row("Total Fees", s_all["total_fee_usd"], s_skip["total_fee_usd"])
    print_row("Total Slippage", s_all["total_slippage_usd"], s_skip["total_slippage_usd"])
    print_row("SL Hits", s_all["sl_hit_count"], s_skip["sl_hit_count"], fmt="d", prefix="")
    print_row("Time Exits", s_all["time_exit_count"], s_skip["time_exit_count"], fmt="d", prefix="")
    sl_pct_all  = s_all["sl_hit_count"] / s_all["total_trades"] * 100 if s_all["total_trades"] else 0
    sl_pct_skip = s_skip["sl_hit_count"] / s_skip["total_trades"] * 100 if s_skip["total_trades"] else 0
    print_row("SL Hit Rate %", sl_pct_all, sl_pct_skip, fmt=".1f", prefix="", suffix="%")
    print_row("Avg Hold Time (min)", s_all["avg_hold_minutes"], s_skip["avg_hold_minutes"], fmt=".1f", prefix="", suffix="")
    print_row("Avg Entry Premium", s_all["avg_entry_premium"], s_skip["avg_entry_premium"])

    print(f"\n{sep}")

    # ======================================================================
    # Weekend-specific analysis
    # ======================================================================
    print(f"\n{'=' * 90}")
    print("   WEEKEND TRADES ANALYSIS (from 'All Days' backtest)")
    print(f"{'=' * 90}")
    print(f"  Weekend trades:       {n_weekend}")
    print(f"  Weekend Net P&L:      ${weekend_net_pnl:+,.2f}")
    print(f"  Weekend Avg P&L:      ${weekend_avg_pnl:+,.2f}")
    print(f"  Weekend Win Rate:     {weekend_win_rate:.1f}% ({weekend_win_count}W / {weekend_loss_count}L)")
    print(f"  Weekend SL Hits:      {weekend_sl_hits}")
    print(f"  Weekend % of total:   {n_weekend/s_all['total_trades']*100:.1f}% of trades")
    if weekend_net_pnl != 0 and s_all["total_pnl_usd"] != 0:
        print(f"  Weekend % of P&L:     {weekend_net_pnl/s_all['total_pnl_usd']*100:+.1f}% of total net P&L")

    print(f"\n  {'Saturday':}")
    if len(sat_df) > 0:
        print(f"    Trades:    {len(sat_df)}")
        print(f"    Net P&L:   ${sat_df['net_pnl_usd'].sum():+,.2f}")
        print(f"    Avg P&L:   ${sat_df['net_pnl_usd'].mean():+,.2f}")
        sat_wins = int((sat_df["net_pnl_usd"] > 0).sum())
        print(f"    Win Rate:  {sat_wins/len(sat_df)*100:.1f}%")
        sat_sl = int((sat_df["exit_reason"] == "sl_hit").sum())
        print(f"    SL Hits:   {sat_sl}")
    else:
        print(f"    No Saturday trades")

    print(f"\n  {'Sunday':}")
    if len(sun_df) > 0:
        print(f"    Trades:    {len(sun_df)}")
        print(f"    Net P&L:   ${sun_df['net_pnl_usd'].sum():+,.2f}")
        print(f"    Avg P&L:   ${sun_df['net_pnl_usd'].mean():+,.2f}")
        sun_wins = int((sun_df["net_pnl_usd"] > 0).sum())
        print(f"    Win Rate:  {sun_wins/len(sun_df)*100:.1f}%")
        sun_sl = int((sun_df["exit_reason"] == "sl_hit").sum())
        print(f"    SL Hits:   {sun_sl}")
    else:
        print(f"    No Sunday trades")

    # Weekday comparison
    print(f"\n  {'Weekday (Mon-Fri) from All Days backtest':}")
    if n_weekday > 0:
        weekday_wins = int((weekday_df["net_pnl_usd"] > 0).sum())
        weekday_wr = weekday_wins / n_weekday * 100
        weekday_sl = int((weekday_df["exit_reason"] == "sl_hit").sum())
        print(f"    Trades:    {n_weekday}")
        print(f"    Net P&L:   ${weekday_net_pnl:+,.2f}")
        print(f"    Avg P&L:   ${weekday_avg_pnl:+,.2f}")
        print(f"    Win Rate:  {weekday_wr:.1f}%")
        print(f"    SL Hits:   {weekday_sl}")

    # ======================================================================
    # Day-of-week breakdown from All Days backtest
    # ======================================================================
    print(f"\n{'=' * 90}")
    print("   DAY-OF-WEEK BREAKDOWN (All Days backtest)")
    print(f"{'=' * 90}")
    df_dow = df_all.copy()
    df_dow["day_name"] = pd.to_datetime(df_dow["date"]).dt.day_name()
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    header_dow = f"  {'Day':<12} {'Trades':>8} {'Net P&L':>14} {'Avg P&L':>12} {'Win Rate':>10} {'SL Hits':>8} {'Avg Premium':>12}"
    print(header_dow)
    print(f"  {'-' * 80}")
    for day in dow_order:
        day_data = df_dow[df_dow["day_name"] == day]
        if len(day_data) == 0:
            continue
        nt = len(day_data)
        net = day_data["net_pnl_usd"].sum()
        avg = day_data["net_pnl_usd"].mean()
        wr = (day_data["net_pnl_usd"] > 0).sum() / nt * 100
        sl = (day_data["exit_reason"] == "sl_hit").sum()
        avg_prem = day_data["entry_premium"].mean()
        marker = " ◄ WEEKEND" if day in ("Saturday", "Sunday") else ""
        print(f"  {day:<12} {nt:>8} ${net:>+12,.2f} ${avg:>+10,.2f} {wr:>9.1f}% {sl:>8} ${avg_prem:>10.2f}{marker}")

    # ======================================================================
    # Monthly comparison
    # ======================================================================
    print(f"\n{'=' * 90}")
    print("   MONTHLY NET P&L COMPARISON")
    print(f"{'=' * 90}")
    m_all = s_all["monthly_pnl"]
    m_skip = s_skip["monthly_pnl"]
    all_months = sorted(set(list(m_all.index) + list(m_skip.index)))

    header_m = f"  {'Month':<12} {'All Days':>14} {'Skip Weekends':>14} {'Weekend P&L':>14} {'Better?':>12}"
    print(header_m)
    print(f"  {'-' * 68}")
    for m in all_months:
        v_all = float(m_all.get(m, 0.0))
        v_skip = float(m_skip.get(m, 0.0))
        v_weekend = v_all - v_skip
        better = "Skip WE" if v_skip > v_all else ("All Days" if v_all > v_skip else "Same")
        print(f"  {str(m):<12} ${v_all:>+12,.2f} ${v_skip:>+12,.2f} ${v_weekend:>+12,.2f} {better:>12}")

    # Count months where skip is better
    skip_better_count = sum(1 for m in all_months
                           if float(m_skip.get(m, 0.0)) > float(m_all.get(m, 0.0)))
    all_better_count = sum(1 for m in all_months
                          if float(m_all.get(m, 0.0)) > float(m_skip.get(m, 0.0)))
    print(f"\n  Skip Weekends better in {skip_better_count}/{len(all_months)} months")
    print(f"  All Days better in {all_better_count}/{len(all_months)} months")

    # ======================================================================
    # RECOMMENDATION
    # ======================================================================
    print(f"\n{'=' * 90}")
    print("   RECOMMENDATION")
    print(f"{'=' * 90}")

    # Score factors
    factors = []

    # 1. Total P&L
    if s_skip["total_pnl_usd"] > s_all["total_pnl_usd"]:
        factors.append(("Total Net P&L", "SKIP", f"Skip=${s_skip['total_pnl_usd']:+,.2f} vs All=${s_all['total_pnl_usd']:+,.2f}"))
    else:
        factors.append(("Total Net P&L", "ALL", f"All=${s_all['total_pnl_usd']:+,.2f} vs Skip=${s_skip['total_pnl_usd']:+,.2f}"))

    # 2. Sharpe
    if extra_skip.get("sortino_ratio", 0) > extra_all.get("sortino_ratio", 0):
        factors.append(("Sortino Ratio", "SKIP", f"Skip={extra_skip['sortino_ratio']:.2f} vs All={extra_all['sortino_ratio']:.2f}"))
    else:
        factors.append(("Sortino Ratio", "ALL", f"All={extra_all['sortino_ratio']:.2f} vs Skip={extra_skip['sortino_ratio']:.2f}"))

    # 3. Max Drawdown (lower is better)
    if abs(s_skip["max_drawdown_pct"]) < abs(s_all["max_drawdown_pct"]):
        factors.append(("Max Drawdown", "SKIP", f"Skip={s_skip['max_drawdown_pct']:.1f}% vs All={s_all['max_drawdown_pct']:.1f}%"))
    else:
        factors.append(("Max Drawdown", "ALL", f"All={s_all['max_drawdown_pct']:.1f}% vs Skip={s_skip['max_drawdown_pct']:.1f}%"))

    # 4. Win Rate
    if s_skip["win_rate_pct"] > s_all["win_rate_pct"]:
        factors.append(("Win Rate", "SKIP", f"Skip={s_skip['win_rate_pct']:.1f}% vs All={s_all['win_rate_pct']:.1f}%"))
    else:
        factors.append(("Win Rate", "ALL", f"All={s_all['win_rate_pct']:.1f}% vs Skip={s_skip['win_rate_pct']:.1f}%"))

    # 5. Profit Factor
    pf_all = s_all["profit_factor"] if not np.isinf(s_all["profit_factor"]) else 999
    pf_skip = s_skip["profit_factor"] if not np.isinf(s_skip["profit_factor"]) else 999
    if pf_skip > pf_all:
        factors.append(("Profit Factor", "SKIP", f"Skip={s_skip['profit_factor']:.2f} vs All={s_all['profit_factor']:.2f}"))
    else:
        factors.append(("Profit Factor", "ALL", f"All={s_all['profit_factor']:.2f} vs Skip={s_skip['profit_factor']:.2f}"))

    # 6. Avg P&L per trade
    if s_skip["avg_pnl_per_trade"] > s_all["avg_pnl_per_trade"]:
        factors.append(("Avg P&L/Trade", "SKIP", f"Skip=${s_skip['avg_pnl_per_trade']:+,.2f} vs All=${s_all['avg_pnl_per_trade']:+,.2f}"))
    else:
        factors.append(("Avg P&L/Trade", "ALL", f"All=${s_all['avg_pnl_per_trade']:+,.2f} vs Skip=${s_skip['avg_pnl_per_trade']:+,.2f}"))

    # 7. Weekend P&L direction
    if weekend_net_pnl > 0:
        factors.append(("Weekend P&L", "ALL", f"Weekends are profitable: ${weekend_net_pnl:+,.2f}"))
    else:
        factors.append(("Weekend P&L", "SKIP", f"Weekends lose money: ${weekend_net_pnl:+,.2f}"))

    # 8. Calmar ratio
    if s_skip["calmar_ratio"] > s_all["calmar_ratio"]:
        factors.append(("Calmar Ratio", "SKIP", f"Skip={s_skip['calmar_ratio']:.2f} vs All={s_all['calmar_ratio']:.2f}"))
    else:
        factors.append(("Calmar Ratio", "ALL", f"All={s_all['calmar_ratio']:.2f} vs Skip={s_skip['calmar_ratio']:.2f}"))

    skip_wins = sum(1 for _, w, _ in factors if w == "SKIP")
    all_wins  = sum(1 for _, w, _ in factors if w == "ALL")

    print(f"\n  Factor Analysis ({skip_wins} SKIP vs {all_wins} ALL):\n")
    for name, winner, detail in factors:
        icon = "✅" if winner == "SKIP" else "❌"
        print(f"    {icon} {name:<20} → {winner:<6} ({detail})")

    winner = "skip_weekends = True" if skip_wins > all_wins else "skip_weekends = False"
    print(f"\n  {'─' * 70}")
    if skip_wins > all_wins:
        print(f"  ✅ RECOMMENDATION: Use skip_weekends = True")
        print(f"     Weekend trades are dragging performance. Skipping them improves")
        print(f"     risk-adjusted returns and overall profitability.")
    elif all_wins > skip_wins:
        print(f"  ✅ RECOMMENDATION: Use skip_weekends = False (trade all days)")
        print(f"     Weekend trades contribute positively. Including them improves")
        print(f"     overall performance across multiple metrics.")
    else:
        print(f"  ⚖️  RECOMMENDATION: Marginal difference. Either setting works.")
        print(f"     Results are close enough that other factors (like operational")
        print(f"     convenience) should guide your decision.")
    print(f"  {'─' * 70}")

    # Save results as JSON for artifact
    results = {
        "all_days": {k: v for k, v in s_all.items() if not isinstance(v, (pd.Series, pd.DataFrame))},
        "skip_weekends": {k: v for k, v in s_skip.items() if not isinstance(v, (pd.Series, pd.DataFrame))},
        "weekend_analysis": {
            "n_trades": n_weekend,
            "net_pnl": weekend_net_pnl,
            "avg_pnl": weekend_avg_pnl,
            "win_rate": weekend_win_rate,
            "sl_hits": weekend_sl_hits,
        },
        "recommendation": winner,
    }

    out_path = REPORTS_DIR / "weekend_comparison_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {out_path}")
    print()


if __name__ == "__main__":
    main()
