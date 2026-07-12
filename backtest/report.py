"""
backtest/report.py
==================
Generates a rich self-contained HTML report with 10 embedded Plotly charts,
KPI summary cards, and a trade log table.  Also exports trades.csv and
equity_curve.csv.

Charts:
  1.  Equity curve
  2.  Daily P&L bars
  3.  Drawdown area
  4.  Monthly P&L heatmap
  5.  P&L distribution histogram
  6.  Entry premium vs P&L scatter
  7.  Hold-time distribution
  8.  Exit reason donut
  9.  Day-of-week average P&L
  10. Cumulative wins vs losses
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

from .config import BacktestConfig
from .portfolio import Portfolio

log = logging.getLogger(__name__)

try:
    import plotly.graph_objects as go
    import plotly.io as pio
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
    log.warning("plotly not installed — HTML charts will be skipped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _c(val: float) -> str:
    return "#26a69a" if val >= 0 else "#ef5350"


def _monthly_pivot(monthly_series: pd.Series) -> pd.DataFrame:
    df = monthly_series.reset_index()
    df.columns = ["period", "pnl"]
    df["year"]  = df["period"].dt.year.astype(str)
    df["month"] = df["period"].dt.month
    pivot = df.pivot(index="year", columns="month", values="pnl").fillna(0)
    month_names = ["Jan","Feb","Mar","Apr","May","Jun",
                   "Jul","Aug","Sep","Oct","Nov","Dec"]
    pivot.columns = [month_names[m - 1] for m in pivot.columns]
    return pivot


def _html(fig: "go.Figure") -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       config={"displayModeBar": False})


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------

def _equity_chart(df: pd.DataFrame, capital: float) -> "go.Figure":
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=df["equity"], name="Equity",
        mode="lines", line=dict(color="#42A5F5", width=2.5),
        fill="tozeroy", fillcolor="rgba(66,165,245,0.07)",
    ))
    fig.add_hline(y=capital, line_dash="dash",
                  line_color="rgba(255,255,255,0.25)",
                  annotation_text="Initial Capital")
    fig.update_layout(title="Equity Curve", xaxis_title="Date",
                      yaxis_title="Equity (USD)", template="plotly_dark", height=380)
    return fig


def _daily_pnl_chart(df: pd.DataFrame) -> "go.Figure":
    fig = go.Figure(go.Bar(
        x=df["date"], y=df["pnl_usd"],
        marker_color=[_c(v) for v in df["pnl_usd"]],
    ))
    fig.update_layout(title="Daily P&L", xaxis_title="Date",
                      yaxis_title="P&L (USD)", template="plotly_dark", height=320)
    return fig


def _drawdown_chart(df: pd.DataFrame) -> "go.Figure":
    dd = df["equity"] - df["equity"].cummax()
    fig = go.Figure(go.Scatter(
        x=df["date"], y=dd, fill="tozeroy",
        fillcolor="rgba(239,83,80,0.2)",
        line=dict(color="#ef5350", width=1.5),
    ))
    fig.update_layout(title="Drawdown (USD)", xaxis_title="Date",
                      yaxis_title="Drawdown (USD)", template="plotly_dark", height=280)
    return fig


def _monthly_heatmap(monthly_series: pd.Series) -> "go.Figure":
    pivot = _monthly_pivot(monthly_series)
    text  = pivot.map(lambda v: f"${v:+,.0f}")
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns.tolist(), y=pivot.index.tolist(),
        text=text.values, texttemplate="%{text}",
        colorscale=[[0.0,"#ef5350"],[0.5,"#1a1a2e"],[1.0,"#26a69a"]],
        zmid=0, showscale=True,
    ))
    fig.update_layout(title="Monthly P&L Heatmap (USD)",
                      xaxis_title="Month", yaxis_title="Year",
                      template="plotly_dark", height=300)
    return fig


def _histogram_chart(df: pd.DataFrame) -> "go.Figure":
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=df[df["pnl_usd"] >= 0]["pnl_usd"],
                               name="Wins", marker_color="#26a69a", nbinsx=30, opacity=0.8))
    fig.add_trace(go.Histogram(x=df[df["pnl_usd"] <  0]["pnl_usd"],
                               name="Losses", marker_color="#ef5350", nbinsx=30, opacity=0.8))
    fig.update_layout(title="P&L Distribution", barmode="overlay",
                      xaxis_title="P&L (USD)", yaxis_title="Count",
                      template="plotly_dark", height=300)
    return fig


def _scatter_chart(df: pd.DataFrame) -> "go.Figure":
    fig = go.Figure(go.Scatter(
        x=df["entry_premium"], y=df["pnl_usd"], mode="markers",
        marker=dict(color=[_c(v) for v in df["pnl_usd"]], size=6, opacity=0.7),
        text=df["date"].astype(str),
        hovertemplate="Date: %{text}<br>Premium: $%{x:.2f}<br>P&L: $%{y:.2f}",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="rgba(255,255,255,0.2)")
    fig.update_layout(title="Entry Premium vs P&L",
                      xaxis_title="Entry Premium (USD)", yaxis_title="P&L (USD)",
                      template="plotly_dark", height=320)
    return fig


def _hold_time_chart(df: pd.DataFrame) -> "go.Figure":
    fig = go.Figure(go.Histogram(x=df["hold_minutes"], nbinsx=25,
                                 marker_color="#7E57C2", opacity=0.85))
    fig.update_layout(title="Hold Time Distribution",
                      xaxis_title="Hold Time (min)", yaxis_title="Count",
                      template="plotly_dark", height=280)
    return fig


def _exit_donut_chart(exit_counts: dict) -> "go.Figure":
    labels = list(exit_counts.keys())
    values = list(exit_counts.values())
    colors = ["#26a69a", "#ef5350", "#FFA726", "#42A5F5"]
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.55,
                           marker=dict(colors=colors[:len(labels)])))
    fig.update_layout(title="Exit Reason Breakdown",
                      template="plotly_dark", height=300)
    return fig


def _dow_chart(dow_avg: pd.Series) -> "go.Figure":
    order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    dow_avg = dow_avg.reindex([d for d in order if d in dow_avg.index])
    fig = go.Figure(go.Bar(
        x=dow_avg.index, y=dow_avg.values,
        marker_color=[_c(v) for v in dow_avg.values],
    ))
    fig.update_layout(title="Avg P&L by Day of Week",
                      xaxis_title="Day", yaxis_title="Avg P&L (USD)",
                      template="plotly_dark", height=280)
    return fig


def _cumulative_wl_chart(df: pd.DataFrame) -> "go.Figure":
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=(df["pnl_usd"] > 0).cumsum(),
        name="Wins", line=dict(color="#26a69a"),
    ))
    fig.add_trace(go.Scatter(
        x=df["date"], y=(df["pnl_usd"] <= 0).cumsum(),
        name="Losses", line=dict(color="#ef5350"),
    ))
    fig.update_layout(title="Cumulative Wins vs Losses",
                      xaxis_title="Date", yaxis_title="Count",
                      template="plotly_dark", height=280)
    return fig


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#0a0e1a;color:#e0e6f0;min-height:100vh;padding-bottom:60px}
.hero{background:linear-gradient(135deg,#0d1b40 0%,#1a0d40 50%,#0d2040 100%);
      padding:48px 40px 36px;border-bottom:1px solid rgba(255,255,255,0.07)}
.hero h1{font-size:2rem;font-weight:700;
  background:linear-gradient(90deg,#42a5f5,#ab47bc);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero .meta{font-size:.85rem;color:#7986a0;margin-top:6px}
.container{max-width:1280px;margin:0 auto;padding:0 32px}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;padding:32px 0 8px}
.kpi-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
          border-radius:12px;padding:20px 22px;transition:transform .15s}
.kpi-card:hover{transform:translateY(-2px);border-color:rgba(66,165,245,.3)}
.kpi-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.8px;color:#7986a0}
.kpi-value{font-size:1.5rem;font-weight:700;margin-top:4px}
.pos{color:#26a69a}.neg{color:#ef5350}.neu{color:#e0e6f0}
.section{margin-top:40px}
.section-title{font-size:1.05rem;font-weight:600;color:#90caf9;
  border-left:3px solid #42a5f5;padding-left:12px;margin-bottom:16px}
.chart-row{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
.chart-row-3{grid-template-columns:1fr 1fr 1fr}
.chart-full{margin-bottom:20px}
.chart-box{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);
           border-radius:12px;padding:4px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:.8rem;
      background:rgba(255,255,255,.02);border-radius:10px;overflow:hidden}
thead{background:rgba(66,165,245,.12)}
th{padding:10px 14px;text-align:left;color:#90caf9;font-weight:600;
   font-size:.75rem;text-transform:uppercase;letter-spacing:.5px}
td{padding:9px 14px;border-bottom:1px solid rgba(255,255,255,.05)}
tr:hover td{background:rgba(255,255,255,.03)}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600}
.tag.sl{background:rgba(239,83,80,.2);color:#ef5350}
.tag.te{background:rgba(38,166,154,.2);color:#26a69a}
.tag.tef{background:rgba(255,167,38,.2);color:#FFA726}
.footer{margin-top:60px;text-align:center;font-size:.78rem;color:#4a5568}
</style>
"""


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def _kpi(label: str, value: str, css: str = "neu") -> str:
    return (f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value {css}">{value}</div></div>')


def _tag(reason: str) -> str:
    cls = {"sl_hit": "sl", "time_exit": "te", "time_exit_fallback": "tef"}.get(reason, "te")
    lbl = {"sl_hit": "SL HIT", "time_exit": "TIME EXIT",
           "time_exit_fallback": "TIME EXIT"}.get(reason, reason)
    return f'<span class="tag {cls}">{lbl}</span>'


class ReportGenerator:

    def __init__(self, cfg: BacktestConfig, portfolio: Portfolio):
        self.cfg   = cfg
        self.port  = portfolio
        self.df    = portfolio.trade_df
        self.stats = portfolio.stats

    def generate(self, out_dir: Path) -> Path:
        """Build HTML + CSVs in out_dir. Returns path to HTML report."""
        out_dir.mkdir(parents=True, exist_ok=True)

        self.df.to_csv(out_dir / "trades.csv", index=False)
        self.df[["date", "equity", "cumulative_pnl"]].to_csv(
            out_dir / "equity_curve.csv", index=False
        )
        log.info("Saved trades.csv and equity_curve.csv → %s", out_dir)

        html_path = out_dir / "report.html"
        html_path.write_text(self._build_html(), encoding="utf-8")
        log.info("HTML report → %s", html_path)
        return html_path

    def _build_html(self) -> str:
        s  = self.stats
        df = self.df
        tp = s["total_pnl_usd"]
        pc = "pos" if tp >= 0 else "neg"
        sr_c = "pos" if s["sharpe_ratio"] >= 1 else ("neg" if s["sharpe_ratio"] < 0 else "neu")

        kpis = "".join([
            _kpi("Total P&L",       f"${tp:+,.2f}",                 pc),
            _kpi("Total Return",    f"{s['total_return_pct']:+.2f}%", pc),
            _kpi("Final Equity",    f"${s['final_equity']:,.2f}",    "neu"),
            _kpi("Win Rate",        f"{s['win_rate_pct']:.1f}%",
                 "pos" if s["win_rate_pct"] >= 50 else "neg"),
            _kpi("Total Trades",    str(s["total_trades"]),          "neu"),
            _kpi("Sharpe Ratio",    f"{s['sharpe_ratio']:.2f}",      sr_c),
            _kpi("Max Drawdown",    f"${s['max_drawdown_usd']:+,.2f}","neg"),
            _kpi("Profit Factor",   f"{s['profit_factor']:.2f}",
                 "pos" if s["profit_factor"] >= 1 else "neg"),
            _kpi("Calmar Ratio",    f"{s['calmar_ratio']:.2f}",      "neu"),
            _kpi("Avg P&L/Trade",   f"${s['avg_pnl_per_trade']:+.2f}",
                 "pos" if s["avg_pnl_per_trade"] >= 0 else "neg"),
            _kpi("Avg Hold Time",   f"{s['avg_hold_minutes']:.1f} min","neu"),
            _kpi("SL Hits",         str(s["sl_hit_count"]),          "neg"),
        ])

        charts = ""
        if PLOTLY_AVAILABLE:
            cdn = ('<script src="https://cdn.plot.ly/plotly-2.29.1.min.js"'
                   ' charset="utf-8"></script>')
            charts = f"""
            {cdn}
            <div class="section">
              <div class="section-title">Equity &amp; Performance</div>
              <div class="chart-full chart-box">{_html(_equity_chart(df, self.cfg.initial_capital))}</div>
              <div class="chart-row">
                <div class="chart-box">{_html(_daily_pnl_chart(df))}</div>
                <div class="chart-box">{_html(_drawdown_chart(df))}</div>
              </div>
            </div>
            <div class="section">
              <div class="section-title">Monthly Breakdown</div>
              <div class="chart-full chart-box">{_html(_monthly_heatmap(s["monthly_pnl"]))}</div>
            </div>
            <div class="section">
              <div class="section-title">Trade Analysis</div>
              <div class="chart-row">
                <div class="chart-box">{_html(_histogram_chart(df))}</div>
                <div class="chart-box">{_html(_scatter_chart(df))}</div>
              </div>
              <div class="chart-row chart-row-3">
                <div class="chart-box">{_html(_hold_time_chart(df))}</div>
                <div class="chart-box">{_html(_exit_donut_chart(s["exit_reason_counts"]))}</div>
                <div class="chart-box">{_html(_dow_chart(s["dow_avg_pnl"]))}</div>
              </div>
              <div class="chart-full chart-box">{_html(_cumulative_wl_chart(df))}</div>
            </div>
            """

        # Trade log (newest 100)
        show = df.sort_values("date", ascending=False).head(100)
        rows = ""
        for _, r in show.iterrows():
            col = "#26a69a" if r["pnl_usd"] >= 0 else "#ef5350"
            rows += (
                f"<tr>"
                f"<td>{pd.Timestamp(r['date']).date()}</td>"
                f"<td>{int(r['atm_strike']):,}</td>"
                f"<td>${r['entry_premium']:.2f}</td>"
                f"<td>${r['exit_premium']:.2f}</td>"
                f"<td style='color:{col};font-weight:600'>${r['pnl_usd']:+,.2f}</td>"
                f"<td>{r['hold_minutes']:.0f} min</td>"
                f"<td>{_tag(r['exit_reason'])}</td>"
                f"</tr>"
            )

        gen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        period = f"{df['date'].min().date()} → {df['date'].max().date()}"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>BTC Short Straddle Backtest Report</title>
  {_CSS}
</head>
<body>
<div class="hero">
  <div class="container">
    <h1>⚡ BTC Short Straddle Backtest</h1>
    <div class="meta">
      Period: {period} &nbsp;|&nbsp; Capital: ${self.cfg.initial_capital:,.0f}
      &nbsp;|&nbsp; Lot Size: {self.cfg.lot_size} &nbsp;|&nbsp;
      SL: {self.cfg.sl_pct:.0f}% of premium &nbsp;|&nbsp; Generated: {gen} UTC
    </div>
  </div>
</div>
<div class="container">
  <div class="kpi-grid">{kpis}</div>
  {charts}
  <div class="section">
    <div class="section-title">Trade Log (Latest 100)</div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>Date</th><th>ATM Strike</th><th>Entry Premium</th>
          <th>Exit Premium</th><th>P&amp;L (USD)</th>
          <th>Hold Time</th><th>Exit Reason</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
  <div class="footer">BTC Short Straddle Backtest · opt-algo · {gen}</div>
</div>
</body>
</html>"""
