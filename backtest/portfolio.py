"""
backtest/portfolio.py
=====================
Aggregates TradeResult objects into portfolio-level statistics.

Uses net_pnl_usd (gross P&L - fees - slippage) as the primary metric for all
equity curve, drawdown, Sharpe, and Calmar calculations.

Also provides slippage_sensitivity() which returns a DataFrame showing key
metrics across a range of slippage assumptions without re-running the backtest.
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
                "pnl_usd":       t.pnl_usd,       # gross (no fees/slippage)
                "fee_usd":       t.fee_usd,
                "slippage_usd":  t.slippage_usd,
                "net_pnl_usd":   t.net_pnl_usd,   # primary metric
                "exit_reason":   t.exit_reason,
                "lot_size":      t.lot_size,
                "sl_threshold":  t.sl_threshold,
                "hold_minutes":  (t.exit_ts - t.entry_ts).total_seconds() / 60,
            })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)

        # Equity curve uses NET P&L
        df["cumulative_pnl"] = df["net_pnl_usd"].cumsum()
        df["equity"]         = self.cfg.initial_capital + df["cumulative_pnl"]
        df["return_pct"]     = df["net_pnl_usd"] / self.cfg.initial_capital * 100.0

        self._df    = df
        self._stats = self._compute_stats(df)

    def _compute_stats(self, df: pd.DataFrame, pnl_col: str = "net_pnl_usd") -> dict:
        """Compute all statistics using the specified P&L column."""
        pnl    = df[pnl_col]
        equity = self.cfg.initial_capital + pnl.cumsum()
        ret    = pnl / self.cfg.initial_capital * 100.0

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

        # Monthly breakdown (on net P&L)
        df2 = df.copy()
        df2["ym"] = df2["date"].dt.to_period("M")
        monthly = df2.groupby("ym")[pnl_col].sum()

        # Day-of-week breakdown
        df2["dow"] = df2["date"].dt.day_name()
        dow_pnl    = df2.groupby("dow")[pnl_col].mean()

        # Fee and slippage totals
        total_fee      = float(df["fee_usd"].sum())       if "fee_usd"      in df.columns else 0.0
        total_slippage = float(df["slippage_usd"].sum())  if "slippage_usd" in df.columns else 0.0
        gross_total    = float(df["pnl_usd"].sum())       if "pnl_usd"      in df.columns else float(pnl.sum())

        return {
            "total_trades":       total_trades,
            "gross_pnl_usd":      gross_total,
            "total_fee_usd":      total_fee,
            "total_slippage_usd": total_slippage,
            "total_pnl_usd":      float(pnl.sum()),       # net
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
    def slippage_sensitivity(
        self,
        slippage_levels: list[float] | None = None,
    ) -> pd.DataFrame:
        """
        Compute key metrics at multiple slippage levels without re-running the backtest.

        For each slippage level s (%), the adjusted net P&L per trade is:
            net = gross_pnl - fee - (entry_premium + exit_premium) * s/100 * lot_size

        Returns a DataFrame with one row per slippage level.
        """
        if slippage_levels is None:
            slippage_levels = [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0]

        df = self._df.copy()
        records = []

        for s_pct in slippage_levels:
            s = s_pct / 100.0
            # Additional slippage cost vs base slippage already baked in
            base_s = self.cfg.slippage_pct / 100.0
            delta_s = s - base_s   # extra slippage on top of what's already calculated
            is_sl_hit = (df["exit_reason"] == "sl_hit")
            multiplier_premium = np.where(
                is_sl_hit,
                df["entry_premium"] + df["exit_premium"],
                df["entry_premium"]
            )
            extra_slip = multiplier_premium * delta_s * df["lot_size"] * self.cfg.contract_value
            adj_net = df["net_pnl_usd"] - extra_slip

            equity   = self.cfg.initial_capital + adj_net.cumsum()
            winners  = adj_net[adj_net > 0]
            losers   = adj_net[adj_net < 0]
            total    = float(adj_net.sum())
            ret_pct  = adj_net / self.cfg.initial_capital * 100.0
            daily_r  = ret_pct / 100.0

            peak     = equity.cummax()
            dd_pct   = float(((equity - peak) / peak).min() * 100)

            sharpe   = (
                daily_r.mean() / daily_r.std() * np.sqrt(252)
                if daily_r.std() > 0 else 0.0
            )
            pf       = (
                float(winners.sum()) / float(abs(losers.sum()))
                if not losers.empty and losers.sum() != 0 else float("inf")
            )
            win_rate = len(winners) / len(adj_net) * 100 if len(adj_net) else 0

            records.append({
                "Slippage":      f"{s_pct:.1f}%",
                "Net P&L":       f"${total:+,.0f}",
                "Return %":      f"{ret_pct.sum():+.1f}%",
                "Win Rate":      f"{win_rate:.1f}%",
                "Profit Factor": f"{pf:.2f}",
                "Sharpe":        f"{sharpe:.2f}",
                "Max DD":        f"{dd_pct:.1f}%",
                "Final Equity":  f"${self.cfg.initial_capital + total:,.0f}",
                # Raw values for chart rendering
                "_net_pnl":      total,
                "_sharpe":       sharpe,
                "_pf":           pf,
                "_dd":           dd_pct,
            })

        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    @property
    def trade_df(self) -> pd.DataFrame:
        return self._df

    @property
    def stats(self) -> dict:
        return self._stats

    def print_summary(self) -> None:
        s   = self._stats
        sep = "-" * 60
        lines = [
            "",
            "=" * 60,
            "   BTC SHORT STRADDLE BACKTEST - SUMMARY (NET of Fees)",
            "=" * 60,
            f"   Period    : {self._df['date'].min().date()} -> {self._df['date'].max().date()}",
            f"   Capital   : ${s['initial_capital']:,.0f}",
            sep,
            f"   Trades    : {s['total_trades']}",
            f"   Win Rate  : {s['win_rate_pct']:.1f}%  ({s['win_count']}W / {s['loss_count']}L)",
            sep,
            f"   Gross P&L   : ${s['gross_pnl_usd']:+,.2f}",
            f"   Total Fees  : ${s['total_fee_usd']:+,.2f}",
            f"   Slippage    : ${s['total_slippage_usd']:+,.2f}",
            f"   Net P&L     : ${s['total_pnl_usd']:+,.2f}  ({s['total_return_pct']:+.2f}%)",
            f"   Final Eq    : ${s['final_equity']:,.2f}",
            sep,
            f"   Avg Net/trade : ${s['avg_pnl_per_trade']:+.2f}",
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
            "=" * 60,
            "",
        ]
        print("\n".join(lines))
