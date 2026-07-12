"""
backtest/portfolio.py
=====================
Aggregates TradeResult objects into portfolio-level statistics.

Computes:
  - Cumulative P&L and equity curve
  - Max drawdown (absolute and %)
  - Sharpe ratio (annualised, daily, risk-free = 0)
  - Calmar ratio
  - Profit factor
  - Win rate, avg win, avg loss, best/worst trade
  - Monthly P&L breakdown
  - Day-of-week average P&L
  - Per-trade log as a DataFrame
"""

from __future__ import annotations

import logging
from typing import List

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .strategy import TradeResult

log = logging.getLogger(__name__)


class Portfolio:
    """Aggregates all trades and computes summary statistics."""

    def __init__(self, cfg: BacktestConfig, trades: List[TradeResult]):
        self.cfg    = cfg
        self.trades = trades
        self._df: pd.DataFrame = pd.DataFrame()
        self._stats: dict = {}

        if trades:
            self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        rows = []
        for t in self.trades:
            rows.append({
                "date":          t.trade_date,
                "atm_strike":    t.atm_strike,
                "spot_estimate": t.spot_estimate,
                "entry_call":    t.entry_call,
                "entry_put":     t.entry_put,
                "entry_premium": t.entry_premium,
                "exit_call":     t.exit_call,
                "exit_put":      t.exit_put,
                "exit_premium":  t.exit_premium,
                "pnl_usd":       t.pnl_usd,
                "exit_reason":   t.exit_reason,
                "lot_size":      t.lot_size,
                "sl_threshold":  t.sl_threshold,
                "hold_minutes":  (t.exit_ts - t.entry_ts).total_seconds() / 60,
            })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)

        df["cumulative_pnl"] = df["pnl_usd"].cumsum()
        df["equity"]         = self.cfg.initial_capital + df["cumulative_pnl"]
        df["return_pct"]     = df["pnl_usd"] / self.cfg.initial_capital * 100.0

        self._df    = df
        self._stats = self._compute_stats(df)

    def _compute_stats(self, df: pd.DataFrame) -> dict:
        pnl    = df["pnl_usd"]
        equity = df["equity"]
        ret    = df["return_pct"]

        total_trades = len(df)
        winners = pnl[pnl > 0]
        losers  = pnl[pnl < 0]

        # Max drawdown
        peak       = equity.cummax()
        dd         = equity - peak
        max_dd_abs = float(dd.min())
        max_dd_pct = float((dd / peak).min() * 100)

        # Sharpe (annualised daily, rf=0)
        daily_ret = ret / 100.0
        sharpe = (
            daily_ret.mean() / daily_ret.std() * np.sqrt(252)
            if daily_ret.std() > 0 else 0.0
        )

        # Calmar
        total_return_pct = float(ret.sum())
        calmar = (
            total_return_pct / abs(max_dd_pct)
            if max_dd_pct != 0 else 0.0
        )

        # Profit factor
        gross_profit = float(winners.sum()) if not winners.empty else 0.0
        gross_loss   = float(abs(losers.sum()))  if not losers.empty else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Exit reason breakdown
        exit_counts = df["exit_reason"].value_counts().to_dict()

        # Monthly breakdown
        df = df.copy()
        df["ym"] = df["date"].dt.to_period("M")
        monthly = df.groupby("ym")["pnl_usd"].sum()

        # Day-of-week breakdown
        df["dow"] = df["date"].dt.day_name()
        dow_pnl   = df.groupby("dow")["pnl_usd"].mean()

        return {
            "total_trades":       total_trades,
            "total_pnl_usd":      float(pnl.sum()),
            "total_return_pct":   total_return_pct,
            "avg_pnl_per_trade":  float(pnl.mean()),
            "win_count":          len(winners),
            "loss_count":         len(losers),
            "win_rate_pct":       len(winners) / total_trades * 100 if total_trades else 0,
            "avg_win_usd":        float(winners.mean()) if not winners.empty else 0.0,
            "avg_loss_usd":       float(losers.mean())  if not losers.empty else 0.0,
            "max_win_usd":        float(pnl.max()),
            "max_loss_usd":       float(pnl.min()),
            "gross_profit_usd":   gross_profit,
            "gross_loss_usd":     gross_loss,
            "profit_factor":      profit_factor,
            "initial_capital":    self.cfg.initial_capital,
            "final_equity":       float(equity.iloc[-1]) if len(equity) else self.cfg.initial_capital,
            "max_drawdown_usd":   max_dd_abs,
            "max_drawdown_pct":   max_dd_pct,
            "sharpe_ratio":       sharpe,
            "calmar_ratio":       calmar,
            "avg_entry_premium":  float(df["entry_premium"].mean()),
            "avg_exit_premium":   float(df["exit_premium"].mean()),
            "avg_hold_minutes":   float(df["hold_minutes"].mean()),
            "sl_hit_count":       exit_counts.get("sl_hit", 0),
            "time_exit_count":    (exit_counts.get("time_exit", 0) +
                                   exit_counts.get("time_exit_fallback", 0)),
            "exit_reason_counts": exit_counts,
            "monthly_pnl":        monthly,
            "dow_avg_pnl":        dow_pnl,
        }

    # ------------------------------------------------------------------
    @property
    def trade_df(self) -> pd.DataFrame:
        return self._df

    @property
    def stats(self) -> dict:
        return self._stats

    def print_summary(self) -> None:
        s   = self._stats
        sep = "-" * 54
        lines = [
            "",
            "=" * 54,
            "   BTC SHORT STRADDLE BACKTEST - SUMMARY",
            "=" * 54,
            f"   Period    : {self._df['date'].min().date()} -> {self._df['date'].max().date()}",
            f"   Capital   : ${s['initial_capital']:,.0f}",
            sep,
            f"   Trades    : {s['total_trades']}",
            f"   Win Rate  : {s['win_rate_pct']:.1f}%  ({s['win_count']}W / {s['loss_count']}L)",
            f"   Total P&L : ${s['total_pnl_usd']:+,.2f}  ({s['total_return_pct']:+.2f}%)",
            f"   Final Eq  : ${s['final_equity']:,.2f}",
            sep,
            f"   Avg P&L/trade : ${s['avg_pnl_per_trade']:+.2f}",
            f"   Avg Win       : ${s['avg_win_usd']:+.2f}",
            f"   Avg Loss      : ${s['avg_loss_usd']:+.2f}",
            f"   Max Win       : ${s['max_win_usd']:+.2f}",
            f"   Max Loss      : ${s['max_loss_usd']:+.2f}",
            f"   Profit Factor : {s['profit_factor']:.2f}",
            sep,
            f"   Sharpe Ratio  : {s['sharpe_ratio']:.2f}",
            f"   Calmar Ratio  : {s['calmar_ratio']:.2f}",
            f"   Max Drawdown  : ${s['max_drawdown_usd']:+.2f}  ({s['max_drawdown_pct']:.1f}%)",
            sep,
            f"   Avg Entry Premium : ${s['avg_entry_premium']:.2f}",
            f"   Avg Hold Time     : {s['avg_hold_minutes']:.1f} min",
            f"   SL Hits / Time Ex : {s['sl_hit_count']} / {s['time_exit_count']}",
            "=" * 54,
            "",
        ]
        print("\n".join(lines))
