"""
backtest/run_xaut_straddle_backtest.py
======================================
Backtesting runner for XAUT Short Straddle options strategy using 1H candles.
Compares strategy performance on Standard Candles vs Heikin Ashi Candles.

Features:
- Automatically downloads/refreshes 1H candle data for XAUT from Delta Exchange.
- Supports both Daily Session (20:30-21:30 IST settlement window) and 1H Hourly execution.
- Generates rich interactive HTML reports with Plotly charts and CSV trade logs.

Usage:
    python backtest/run_xaut_straddle_backtest.py
    python backtest/run_xaut_straddle_backtest.py --mode hourly --capital 1000
    python backtest/run_xaut_straddle_backtest.py --mode daily --sl-pct 50
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Force UTF-8 encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest.candle_utils import convert_to_heikin_ashi
from backtest.xaut_data_loader import XAUTDataLoader, DEFAULT_XAUT_CACHE_PATH
from core.config import Config

log = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"
IST = timezone(timedelta(hours=5, minutes=30))


class XAUTStraddleBacktester:
    """Simulates XAUT short straddle trades on candle datasets."""

    def __init__(
        self,
        initial_capital: float = 1000.0,
        capital_allocation_pct: float = 20.0,
        leverage: float = 100.0,
        sl_pct: Optional[float] = None,
        strike_step: float = 10.0,
        contract_value: float = 0.001,
        fee_rate: float = 0.0001,       # Delta Exchange 0.01% taker fee
        settle_fee_rate: float = 0.0001, # Delta Exchange 0.01% settlement fee
        max_lot_size: int = 1000,
    ):
        self.initial_capital = initial_capital
        self.capital_allocation_pct = capital_allocation_pct
        self.leverage = leverage
        self.sl_pct = sl_pct
        self.strike_step = strike_step
        self.contract_value = contract_value
        self.fee_rate = fee_rate
        self.settle_fee_rate = settle_fee_rate
        self.max_lot_size = max_lot_size

    def run(
        self,
        df: pd.DataFrame,
        candle_type: str = "Standard",
        mode: str = "hourly",
    ) -> Tuple[pd.DataFrame, Dict]:
        """
        Execute backtest on the provided candle DataFrame.
        mode: 'hourly' (every 1h candle) or 'daily' (20:30 IST session only)
        """
        data = df.copy()

        # Compute rolling realized volatility (24-candle window) annualized
        returns = data["close"].pct_change()
        vol_24 = returns.rolling(24).std() * np.sqrt(365 * 24)
        # Apply 1.15x Variance Risk Premium (IV is typically 1.15x RV)
        data["iv"] = (vol_24 * 1.15).fillna(0.18).clip(lower=0.12, upper=0.55)

        if mode == "daily":
            # Match 20:30 IST candle (covers 20:30 to 21:30 IST expiry window)
            trades_df = data[data["time_ist"] == "20:30:00"].copy().reset_index(drop=True)
            tau = 1.0 / (365.0 * 24.0)
        else:
            trades_df = data.copy().reset_index(drop=True)
            tau = 1.0 / (365.0 * 24.0)

        equity = self.initial_capital
        peak_equity = self.initial_capital
        trades: List[Dict] = []

        for idx, row in trades_df.iterrows():
            s0 = float(row["open"])
            s_exit = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            iv = float(row["iv"])

            # ATM Strike
            k = round(s0 / self.strike_step) * self.strike_step

            # Dynamic position sizing based on settings.yaml margin logic
            margin_per_lot = 2.0 * s0 * self.contract_value / self.leverage
            capital_deploy = equity * (self.capital_allocation_pct / 100.0)
            if margin_per_lot > 0:
                lots = max(1, int(capital_deploy / margin_per_lot))
                if self.max_lot_size > 0:
                    lots = min(lots, self.max_lot_size)
            else:
                lots = 100

            # Black-Scholes ATM Straddle entry premium
            prem = 0.79788 * s0 * iv * np.sqrt(tau)

            # Check Stop Loss
            sl_threshold = prem * (1.0 + self.sl_pct / 100.0) if self.sl_pct else None
            max_move = max(abs(high - k), abs(low - k))

            if sl_threshold and max_move >= sl_threshold:
                payoff = sl_threshold
                exit_reason = "sl_hit"
                exit_p = k + (sl_threshold if (high - k) > (k - low) else -sl_threshold)
            else:
                payoff = abs(s_exit - k)
                exit_reason = "time_exit"
                exit_p = s_exit

            # P&L Calculation
            gross_pnl = (prem - payoff) * lots * self.contract_value

            # Trading fees
            entry_fee = 2 * lots * self.contract_value * self.fee_rate * s0
            if exit_reason == "sl_hit":
                exit_fee = 2 * lots * self.contract_value * self.fee_rate * exit_p
            else:
                exit_fee = 1 * lots * self.contract_value * self.settle_fee_rate * exit_p
            fees = entry_fee + exit_fee
            net_pnl = gross_pnl - fees

            equity += net_pnl
            peak_equity = max(peak_equity, equity)
            drawdown_pct = ((equity - peak_equity) / peak_equity) * 100.0

            trades.append({
                "trade_id": idx + 1,
                "date": row["date"],
                "time": row["time_ist"],
                "candle_type": candle_type,
                "spot_entry": round(s0, 2),
                "atm_strike": round(k, 2),
                "spot_exit": round(s_exit, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "iv_pct": round(iv * 100, 1),
                "lots": lots,
                "premium_collected": round(prem, 2),
                "exit_payoff": round(payoff, 2),
                "gross_pnl": round(gross_pnl, 2),
                "fees": round(fees, 2),
                "net_pnl": round(net_pnl, 2),
                "equity": round(equity, 2),
                "drawdown_pct": round(drawdown_pct, 2),
                "exit_reason": exit_reason,
            })

        tdf = pd.DataFrame(trades)
        if tdf.empty:
            return tdf, {}

        # Aggregate Statistics
        wins = tdf[tdf["net_pnl"] > 0]
        losses = tdf[tdf["net_pnl"] <= 0]
        total_trades = len(tdf)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades * 100.0) if total_trades else 0.0

        total_net_pnl = float(tdf["net_pnl"].sum())
        total_gross_pnl = float(tdf["gross_pnl"].sum())
        total_fees = float(tdf["fees"].sum())
        total_return_pct = (total_net_pnl / self.initial_capital) * 100.0

        gross_profit = float(wins["net_pnl"].sum()) if not wins.empty else 0.0
        gross_loss = float(abs(losses["net_pnl"].sum())) if not losses.empty else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # Sharpe ratio
        daily_ret = tdf["net_pnl"] / self.initial_capital
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(365 * 24 if mode == "hourly" else 365)) if daily_ret.std() > 0 else 0.0

        max_dd_pct = float(tdf["drawdown_pct"].min())

        stats = {
            "candle_type": candle_type,
            "mode": mode,
            "initial_capital": self.initial_capital,
            "final_equity": float(equity),
            "total_net_pnl": total_net_pnl,
            "total_gross_pnl": total_gross_pnl,
            "total_fees": total_fees,
            "total_return_pct": total_return_pct,
            "total_trades": total_trades,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": max_dd_pct,
            "avg_pnl_trade": float(tdf["net_pnl"].mean()),
            "avg_win": float(wins["net_pnl"].mean()) if not wins.empty else 0.0,
            "avg_loss": float(losses["net_pnl"].mean()) if not losses.empty else 0.0,
            "max_win": float(tdf["net_pnl"].max()),
            "max_loss": float(tdf["net_pnl"].min()),
            "start_date": f"{tdf['date'].iloc[0]} {tdf['time'].iloc[0]}",
            "end_date": f"{tdf['date'].iloc[-1]} {tdf['time'].iloc[-1]}",
        }

        return tdf, stats


def generate_comparison_report(
    std_trades: pd.DataFrame,
    std_stats: Dict,
    ha_trades: pd.DataFrame,
    ha_stats: Dict,
    out_dir: Path,
    mode_label: str = "1H Hourly Session",
) -> Path:
    """Generate self-contained interactive HTML comparison dashboard."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Equity Curve Comparison Figure
    fig_equity = go.Figure()
    fig_equity.add_trace(go.Scatter(
        x=std_trades.index,
        y=std_trades["equity"],
        mode="lines",
        name="Standard Candle Equity",
        line=dict(color="#22c55e", width=2.5),
        fill="tozeroy",
        fillcolor="rgba(34, 197, 94, 0.08)",
    ))
    fig_equity.add_trace(go.Scatter(
        x=ha_trades.index,
        y=ha_trades["equity"],
        mode="lines",
        name="Heikin Ashi Equity",
        line=dict(color="#ef4444", width=2.5),
        fill="tozeroy",
        fillcolor="rgba(239, 68, 68, 0.08)",
    ))
    fig_equity.add_hline(
        y=std_stats["initial_capital"],
        line_dash="dash",
        line_color="rgba(255, 255, 255, 0.3)",
        annotation_text="Starting Capital ($1,000)",
    )
    fig_equity.update_layout(
        title="Equity Curve: Standard Candles vs Heikin Ashi Candles (USD)",
        xaxis_title="Trade Index",
        yaxis_title="Account Equity ($)",
        template="plotly_dark",
        height=400,
        legend=dict(yanchor="top", y=0.98, xanchor="left", x=0.02),
        margin=dict(l=40, r=40, t=50, b=40),
    )

    # 2. Drawdown Comparison Figure
    fig_dd = go.Figure()
    fig_dd.add_trace(go.Scatter(
        x=std_trades.index,
        y=std_trades["drawdown_pct"],
        mode="lines",
        name="Standard Drawdown %",
        line=dict(color="#38bdf8", width=1.8),
    ))
    fig_dd.add_trace(go.Scatter(
        x=ha_trades.index,
        y=ha_trades["drawdown_pct"],
        mode="lines",
        name="Heikin Ashi Drawdown %",
        line=dict(color="#f97316", width=1.8),
    ))
    fig_dd.update_layout(
        title="Underwater Drawdown (%)",
        xaxis_title="Trade Index",
        yaxis_title="Drawdown (%)",
        template="plotly_dark",
        height=280,
        margin=dict(l=40, r=40, t=50, b=40),
    )

    # Convert plotly figures to HTML divs
    equity_html = fig_equity.to_html(full_html=False, include_plotlyjs=False)
    dd_html = fig_dd.to_html(full_html=False, include_plotlyjs=False)

    # Build Trade rows for table (first 100 trades for preview + export links)
    def make_table_rows(df: pd.DataFrame) -> str:
        rows = []
        for _, r in df.head(100).iterrows():
            pnl_cls = "text-green" if r["net_pnl"] >= 0 else "text-red"
            rows.append(f"""
            <tr>
                <td>{r['trade_id']}</td>
                <td>{r['date']}</td>
                <td>{r['time']}</td>
                <td>${r['spot_entry']:,.2f}</td>
                <td>${r['atm_strike']:,.2f}</td>
                <td>${r['spot_exit']:,.2f}</td>
                <td>{r['lots']}</td>
                <td>${r['premium_collected']:.2f}</td>
                <td>${r['exit_payoff']:.2f}</td>
                <td class="{pnl_cls}" style="font-weight:600;">${r['net_pnl']:+,.2f}</td>
                <td style="font-weight:600;">${r['equity']:,.2f}</td>
                <td><span class="badge {r['exit_reason']}">{r['exit_reason']}</span></td>
            </tr>
            """)
        return "".join(rows)

    std_table_rows = make_table_rows(std_trades)
    ha_table_rows = make_table_rows(ha_trades)

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>XAUT Short Straddle Backtest Report</title>
    <script src="https://cdn.plot.ly/plotly-2.29.1.min.js"></script>
    <style>
        :root {{
            --bg: #090d16;
            --card-bg: #111827;
            --border: #1f2937;
            --text: #f3f4f6;
            --text-muted: #9ca3af;
            --green: #22c55e;
            --red: #ef4444;
            --blue: #38bdf8;
            --gold: #fbbf24;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 30px;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 20px;
            margin-bottom: 25px;
        }}
        .header h1 {{ margin: 0; font-size: 26px; color: var(--gold); }}
        .meta {{ color: var(--text-muted); font-size: 14px; margin-top: 6px; }}
        
        .kpi-section-title {{
            font-size: 16px;
            font-weight: 700;
            margin: 20px 0 12px 0;
            color: var(--blue);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 14px;
            margin-bottom: 25px;
        }}
        .kpi-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }}
        .kpi-label {{
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 6px;
        }}
        .kpi-val {{
            font-size: 22px;
            font-weight: 700;
        }}
        .text-green {{ color: var(--green); }}
        .text-red {{ color: var(--red); }}
        .text-gold {{ color: var(--gold); }}
        
        .chart-box {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 25px;
        }}
        
        .tab-buttons {{
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
        }}
        .tab-btn {{
            background: #1f2937;
            border: 1px solid #374151;
            color: var(--text-muted);
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            font-size: 13px;
        }}
        .tab-btn.active {{
            background: #3b82f6;
            color: #ffffff;
            border-color: #60a5fa;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        th, td {{
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }}
        th {{
            background: #1f2937;
            color: var(--text-muted);
            font-weight: 600;
        }}
        tr:hover {{ background: rgba(255, 255, 255, 0.02); }}
        
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge.time_exit {{ background: rgba(34, 197, 94, 0.2); color: var(--green); }}
        .badge.sl_hit {{ background: rgba(239, 68, 68, 0.2); color: var(--red); }}
        
        .footer {{
            margin-top: 40px;
            text-align: center;
            font-size: 12px;
            color: var(--text-muted);
            border-top: 1px solid var(--border);
            padding-top: 20px;
        }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>XAUT Short Straddle Backtest Report</h1>
            <div class="meta">Underlying: Tether Gold (XAUT) | Mode: {mode_label} | Period: {std_stats['start_date']} to {std_stats['end_date']}</div>
        </div>
        <div style="text-align: right;" class="meta">
            Exchange: Delta Exchange<br>
            Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>

    <!-- COMPARISON TABLE -->
    <div class="kpi-section-title">Head-to-Head Comparison: Standard vs Heikin Ashi</div>
    <div style="overflow-x: auto; margin-bottom: 25px;">
        <table style="background: var(--card-bg); border-radius: 8px; overflow: hidden; border: 1px solid var(--border);">
            <thead>
                <tr>
                    <th>Metric</th>
                    <th style="color: var(--green);">Standard Candles</th>
                    <th style="color: var(--red);">Heikin Ashi Candles</th>
                    <th style="color: var(--blue);">Difference (Std vs HA)</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><b>Total Net P&L</b></td>
                    <td class="{'text-green' if std_stats['total_net_pnl'] >= 0 else 'text-red'}" style="font-size: 16px; font-weight: 700;">${std_stats['total_net_pnl']:+,.2f}</td>
                    <td class="{'text-green' if ha_stats['total_net_pnl'] >= 0 else 'text-red'}" style="font-size: 16px; font-weight: 700;">${ha_stats['total_net_pnl']:+,.2f}</td>
                    <td style="font-weight: 700;">${(std_stats['total_net_pnl'] - ha_stats['total_net_pnl']):+,.2f}</td>
                </tr>
                <tr>
                    <td><b>Total Return %</b></td>
                    <td class="{'text-green' if std_stats['total_return_pct'] >= 0 else 'text-red'}">{std_stats['total_return_pct']:+.2f}%</td>
                    <td class="{'text-green' if ha_stats['total_return_pct'] >= 0 else 'text-red'}">{ha_stats['total_return_pct']:+.2f}%</td>
                    <td>{(std_stats['total_return_pct'] - ha_stats['total_return_pct']):+.2f}%</td>
                </tr>
                <tr>
                    <td><b>Win Rate</b></td>
                    <td>{std_stats['win_rate_pct']:.1f}% ({std_stats['wins']}W / {std_stats['losses']}L)</td>
                    <td>{ha_stats['win_rate_pct']:.1f}% ({ha_stats['wins']}W / {ha_stats['losses']}L)</td>
                    <td>{(std_stats['win_rate_pct'] - ha_stats['win_rate_pct']):+.1f}%</td>
                </tr>
                <tr>
                    <td><b>Profit Factor</b></td>
                    <td>{std_stats['profit_factor']:.2f}</td>
                    <td>{ha_stats['profit_factor']:.2f}</td>
                    <td>{(std_stats['profit_factor'] - ha_stats['profit_factor']):+.2f}</td>
                </tr>
                <tr>
                    <td><b>Sharpe Ratio</b></td>
                    <td>{std_stats['sharpe_ratio']:.2f}</td>
                    <td>{ha_stats['sharpe_ratio']:.2f}</td>
                    <td>{(std_stats['sharpe_ratio'] - ha_stats['sharpe_ratio']):+.2f}</td>
                </tr>
                <tr>
                    <td><b>Max Drawdown %</b></td>
                    <td class="text-red">{std_stats['max_drawdown_pct']:.1f}%</td>
                    <td class="text-red">{ha_stats['max_drawdown_pct']:.1f}%</td>
                    <td>{(std_stats['max_drawdown_pct'] - ha_stats['max_drawdown_pct']):+.1f}%</td>
                </tr>
                <tr>
                    <td><b>Final Equity</b></td>
                    <td>${std_stats['final_equity']:,.2f}</td>
                    <td>${ha_stats['final_equity']:,.2f}</td>
                    <td>${(std_stats['final_equity'] - ha_stats['final_equity']):+,.2f}</td>
                </tr>
                <tr>
                    <td><b>Total Trades</b></td>
                    <td>{std_stats['total_trades']}</td>
                    <td>{ha_stats['total_trades']}</td>
                    <td>0</td>
                </tr>
            </tbody>
        </table>
    </div>

    <!-- CHARTS -->
    <div class="kpi-section-title">Visual Performance & Equity Charts</div>
    <div class="chart-box">{equity_html}</div>
    <div class="chart-box">{dd_html}</div>

    <!-- TRADE LOGS -->
    <div class="kpi-section-title">Trade Execution Logs (Sample 100 Trades)</div>
    <div class="tab-buttons">
        <button class="tab-btn active" onclick="showTab('std')">Standard Candles Trade Log ({len(std_trades)} trades)</button>
        <button class="tab-btn" onclick="showTab('ha')">Heikin Ashi Trade Log ({len(ha_trades)} trades)</button>
    </div>

    <div id="std-log" style="overflow-x: auto; background: var(--card-bg); border-radius: 8px; border: 1px solid var(--border);">
        <table>
            <thead>
                <tr>
                    <th>#</th><th>Date</th><th>Time (IST)</th><th>Spot Entry</th><th>ATM Strike</th><th>Spot Exit</th>
                    <th>Lots</th><th>Premium</th><th>Exit Payoff</th><th>Net P&amp;L</th><th>Equity</th><th>Exit Reason</th>
                </tr>
            </thead>
            <tbody>{std_table_rows}</tbody>
        </table>
    </div>

    <div id="ha-log" style="display: none; overflow-x: auto; background: var(--card-bg); border-radius: 8px; border: 1px solid var(--border);">
        <table>
            <thead>
                <tr>
                    <th>#</th><th>Date</th><th>Time (IST)</th><th>Spot Entry</th><th>ATM Strike</th><th>Spot Exit</th>
                    <th>Lots</th><th>Premium</th><th>Exit Payoff</th><th>Net P&amp;L</th><th>Equity</th><th>Exit Reason</th>
                </tr>
            </thead>
            <tbody>{ha_table_rows}</tbody>
        </table>
    </div>

    <div class="footer">
        XAUT Short Straddle Backtest Report &middot; Generated automatically from Delta Exchange Data
    </div>
</div>

<script>
function showTab(type) {{
    const stdDiv = document.getElementById('std-log');
    const haDiv = document.getElementById('ha-log');
    const btns = document.querySelectorAll('.tab-btn');
    if (type === 'std') {{
        stdDiv.style.display = 'block';
        haDiv.style.display = 'none';
        btns[0].classList.add('active');
        btns[1].classList.remove('active');
    }} else {{
        stdDiv.style.display = 'none';
        haDiv.style.display = 'block';
        btns[0].classList.remove('active');
        btns[1].classList.add('active');
    }}
}}
</script>
</body>
</html>
"""
    out_html = out_dir / "xaut_short_straddle_report.html"
    out_html.write_text(html_template, encoding="utf-8")

    # Export CSV trade logs
    std_trades.to_csv(out_dir / "xaut_straddle_standard_trades.csv", index=False)
    ha_trades.to_csv(out_dir / "xaut_straddle_heikin_ashi_trades.csv", index=False)

    return out_html


def print_terminal_summary(std_stats: Dict, ha_stats: Dict, mode: str):
    """Print an aesthetic side-by-side terminal comparison."""
    sep = "=" * 80
    thin = "-" * 80
    std_wl = f"{std_stats['wins']}W / {std_stats['losses']}L"
    ha_wl = f"{ha_stats['wins']}W / {ha_stats['losses']}L"
    std_gross = f"${std_stats['total_gross_pnl']:+,.2f}"
    ha_gross = f"${ha_stats['total_gross_pnl']:+,.2f}"
    std_fees = f"-${std_stats['total_fees']:,.2f}"
    ha_fees = f"-${ha_stats['total_fees']:,.2f}"
    std_net = f"${std_stats['total_net_pnl']:+,.2f}"
    ha_net = f"${ha_stats['total_net_pnl']:+,.2f}"
    std_ret = f"{std_stats['total_return_pct']:+.1f}%"
    ha_ret = f"{ha_stats['total_return_pct']:+.1f}%"
    std_eq = f"${std_stats['final_equity']:,.2f}"
    ha_eq = f"${ha_stats['final_equity']:,.2f}"
    std_dd = f"{std_stats['max_drawdown_pct']:.1f}%"
    ha_dd = f"{ha_stats['max_drawdown_pct']:.1f}%"
    std_avg = f"${std_stats['avg_pnl_trade']:+.2f}"
    ha_avg = f"${ha_stats['avg_pnl_trade']:+.2f}"

    print(f"\n{sep}")
    print(f"  XAUT SHORT STRADDLE BACKTEST - CANDLE COMPARISON ({mode.upper()})")
    print(f"{sep}")
    print(f"  Period: {std_stats['start_date']} -> {std_stats['end_date']}")
    print(f"  Initial Capital: ${std_stats['initial_capital']:,.2f}")
    print(thin)
    print(f"  {'Metric':<25} {'Standard Candles':>24} {'Heikin Ashi Candles':>24}")
    print(thin)
    print(f"  {'Total Trades':<25} {std_stats['total_trades']:>24} {ha_stats['total_trades']:>24}")
    print(f"  {'Win Rate':<25} {std_stats['win_rate_pct']:>23.1f}% {ha_stats['win_rate_pct']:>23.1f}%")
    print(f"  {'Wins / Losses':<25} {std_wl:>24} {ha_wl:>24}")
    print(thin)
    print(f"  {'Gross P&L':<25} {std_gross:>24} {ha_gross:>24}")
    print(f"  {'Total Fees':<25} {std_fees:>24} {ha_fees:>24}")
    print(f"  {'Net P&L':<25} {std_net:>24} {ha_net:>24}")
    print(f"  {'Net Return':<25} {std_ret:>24} {ha_ret:>24}")
    print(f"  {'Final Equity':<25} {std_eq:>24} {ha_eq:>24}")
    print(thin)
    print(f"  {'Profit Factor':<25} {std_stats['profit_factor']:>24.2f} {ha_stats['profit_factor']:>24.2f}")
    print(f"  {'Sharpe Ratio':<25} {std_stats['sharpe_ratio']:>24.2f} {ha_stats['sharpe_ratio']:>24.2f}")
    print(f"  {'Max Drawdown':<25} {std_dd:>24} {ha_dd:>24}")
    print(f"  {'Avg Net P&L / Trade':<25} {std_avg:>24} {ha_avg:>24}")
    print(sep)


def main():
    parser = argparse.ArgumentParser(description="XAUT Short Straddle Backtest Runner")
    parser.add_argument("--mode", choices=["daily", "hourly", "both"], default="both",
                        help="Straddle execution mode: daily (20:30 IST session), hourly (every 1H), or both")
    parser.add_argument("--capital", type=float, default=1000.0, help="Initial capital in USD")
    parser.add_argument("--alloc-pct", type=float, default=20.0, help="Capital allocation percentage")
    parser.add_argument("--leverage", type=float, default=100.0, help="Leverage multiplier")
    parser.add_argument("--sl-pct", type=float, default=None, help="Stop loss %% of premium (default None)")
    parser.add_argument("--force-fetch", action="store_true", help="Force re-fetch of data from Delta Exchange")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    # 1. Fetch / Load XAUT Data
    loader = XAUTDataLoader()
    df = loader.get_or_fetch_data(force_fetch=args.force_fetch)
    if df.empty:
        log.error("No candle data available for backtest.")
        sys.exit(1)

    log.info("Loaded %d candles of %s data", len(df), loader.symbol)

    # 2. Convert to Heikin Ashi
    ha_df = convert_to_heikin_ashi(df)

    backtester = XAUTStraddleBacktester(
        initial_capital=args.capital,
        capital_allocation_pct=args.alloc_pct,
        leverage=args.leverage,
        sl_pct=args.sl_pct,
    )

    modes = ["daily", "hourly"] if args.mode == "both" else [args.mode]

    for mode in modes:
        mode_title = "Daily Session (20:30 IST)" if mode == "daily" else "Hourly (Every 1H Candle)"
        log.info("Running backtest for mode: %s...", mode_title)

        std_trades, std_stats = backtester.run(df, candle_type="Standard", mode=mode)
        ha_trades, ha_stats = backtester.run(ha_df, candle_type="Heikin Ashi", mode=mode)

        print_terminal_summary(std_stats, ha_stats, mode=mode_title)

        # Generate HTML report and CSVs
        report_sub_dir = REPORTS_DIR / f"xaut_straddle_{mode}"
        report_path = generate_comparison_report(
            std_trades, std_stats,
            ha_trades, ha_stats,
            out_dir=report_sub_dir,
            mode_label=mode_title,
        )
        print(f"\n[OK] Interactive HTML Report generated: file:///{report_path.resolve()}\n")


if __name__ == "__main__":
    main()
