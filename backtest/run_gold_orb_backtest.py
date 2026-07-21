"""
backtest/run_gold_orb_backtest.py
==================================
Backtesting runner for XAUTUSD Gold Opening Range Breakout (ORB) strategy.
Generates CSV trade logs and detailed HTML reports with interactive charts.

Usage:
    python backtest/run_gold_orb_backtest.py
    python backtest/run_gold_orb_backtest.py --file "D:\\Workspace\\crypto-backtest-data\\Futures\\delta_crypto_2026-01-01_to_2026-07-21_1h\\delta_crypto_data\\XAUTUSD_1h.csv" --capital 1000
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.gold_orb_strategy import GoldOrbStrategy

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DATA_PATH = Path(
    r"D:\Workspace\crypto-backtest-data\Futures\delta_crypto_2026-01-01_to_2026-07-21_1h\delta_crypto_data\XAUTUSD_1h.csv"
)


def load_ohlcv_data(filepath: Path) -> pd.DataFrame:
    """Load and preprocess 1H OHLCV CSV data for XAUTUSD."""
    if not filepath.exists():
        raise FileNotFoundError(f"Data file not found: {filepath}")

    df = pd.read_csv(filepath)
    df.columns = [c.strip().lower() for c in df.columns]

    if "date" in df.columns and "time" in df.columns:
        datetime_str = df["date"].astype(str) + " " + df["time"].astype(str)
    elif "date" in df.columns:
        datetime_str = df["date"].astype(str)
    else:
        datetime_str = df["time"].astype(str)

    try:
        df["time"] = pd.to_datetime(datetime_str, utc=True, format="mixed", dayfirst=True)
    except Exception:
        df["time"] = pd.to_datetime(datetime_str, format="mixed", dayfirst=True)

    epoch = pd.Timestamp("1970-01-01", tz="UTC") if df["time"].dt.tz is not None else pd.Timestamp("1970-01-01")
    df["time"] = (df["time"].dt.floor("s") - epoch) // pd.Timedelta("1s")
    df = df.sort_values("time").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.dropna().reset_index(drop=True)


def generate_html_report(metrics: dict, tdf: pd.DataFrame, out_path: Path):
    """Generate a rich, self-contained HTML report for Gold ORB strategy."""
    trades_html = ""
    for idx, row in tdf.iterrows():
        pnl_class = "positive" if row["net_pnl"] >= 0 else "negative"
        exit_class = "tp-hit" if row["exit_type"] == "TP HIT" else "sl-hit"
        trades_html += f"""
        <tr>
            <td>{idx + 1}</td>
            <td>{row['entry_time']}</td>
            <td>{row['exit_time']}</td>
            <td><span class="badge {row['type'].lower()}">{row['type']}</span></td>
            <td>${row['orb_h1']:,.2f}</td>
            <td>${row['orb_l1']:,.2f}</td>
            <td>${row['entry_price']:,.2f}</td>
            <td>${row['exit_price']:,.2f}</td>
            <td>${row['sl']:,.2f}</td>
            <td>${row['tp']:,.2f}</td>
            <td><span class="badge {exit_class}">{row['exit_type']}</span></td>
            <td>${row['gross_pnl']:+,.2f}</td>
            <td>${row['fees']:,.2f}</td>
            <td class="{pnl_class}">${row['net_pnl']:+,.2f}</td>
            <td style="font-weight:600;">${row['equity']:,.2f}</td>
        </tr>
        """

    equity_points = ", ".join(f"{x:.2f}" for x in [metrics["initial_capital"]] + list(tdf["equity"]))
    trade_labels = ", ".join(f"'{i}'" for i in range(len(tdf) + 1))
    pnl_points = ", ".join(f"{x:.2f}" for x in list(tdf["net_pnl"]))

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gold ORB Strategy Backtest Report - XAUTUSD</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg-color: #0d1117;
            --card-bg: #161b22;
            --border-color: #30363d;
            --text-main: #c9d1d9;
            --text-muted: #8b949e;
            --accent-green: #238636;
            --accent-red: #da3633;
            --accent-blue: #58a6ff;
            --accent-gold: #d29922;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 24px;
        }}
        .container {{
            max-width: 1300px;
            margin: 0 auto;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
            color: var(--accent-gold);
        }}
        .header-meta {{
            color: var(--text-muted);
            font-size: 14px;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .kpi-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
        }}
        .kpi-title {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            margin-bottom: 8px;
        }}
        .kpi-value {{
            font-size: 24px;
            font-weight: 700;
        }}
        .positive {{ color: #3fb950; }}
        .negative {{ color: #f85149; }}
        .chart-grid {{
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 16px;
            margin-bottom: 24px;
        }}
        .chart-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
        }}
        .chart-title {{
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 16px;
            color: var(--text-main);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            font-size: 13px;
        }}
        th, td {{
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
        }}
        th {{
            background: #21262d;
            color: var(--text-muted);
            font-weight: 600;
        }}
        tr:hover {{
            background: #1c2128;
        }}
        .badge {{
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge.long {{ background: rgba(35, 134, 54, 0.2); color: #3fb950; }}
        .badge.short {{ background: rgba(218, 54, 51, 0.2); color: #f85149; }}
        .badge.tp-hit {{ background: rgba(35, 134, 54, 0.25); color: #3fb950; }}
        .badge.sl-hit {{ background: rgba(218, 54, 51, 0.25); color: #f85149; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>Gold ORB Backtest Report (XAUTUSD 1H)</h1>
                <div class="header-meta">1H Opening Range Breakout (06:30 IST) | 1:1 Risk/Reward | 1000 Lots | 100x Leverage</div>
            </div>
            <div class="header-meta" style="text-align: right;">
                Generated: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}<br>
                Period: {metrics['start_date']} to {metrics['end_date']}
            </div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-title">Initial Capital</div>
                <div class="kpi-value">${metrics['initial_capital']:,.2f}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Final Equity</div>
                <div class="kpi-value {'positive' if metrics['final_capital'] >= metrics['initial_capital'] else 'negative'}">${metrics['final_capital']:,.2f}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Total Return</div>
                <div class="kpi-value {'positive' if metrics['return_pct'] >= 0 else 'negative'}">{metrics['return_pct']:+.2f}%</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Win Rate</div>
                <div class="kpi-value">{metrics['win_rate']:.2f}%</div>
                <div style="font-size:12px; color:var(--text-muted);">{metrics['wins']} W / {metrics['losses']} L</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Profit Factor</div>
                <div class="kpi-value">{metrics['profit_factor']:.2f}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Max Drawdown</div>
                <div class="kpi-value negative">{metrics['max_drawdown_pct']:.2f}%</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Total Trades</div>
                <div class="kpi-value">{metrics['total_trades']}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Total Fees</div>
                <div class="kpi-value" style="color:var(--accent-gold);">${metrics['total_fees']:,.2f}</div>
            </div>
        </div>

        <div class="chart-grid">
            <div class="chart-card">
                <div class="chart-title">Equity Curve ($)</div>
                <canvas id="equityChart" height="200"></canvas>
            </div>
            <div class="chart-card">
                <div class="chart-title">Trade PnL Distribution ($)</div>
                <canvas id="pnlChart" height="200"></canvas>
            </div>
        </div>

        <div class="chart-card" style="margin-bottom: 24px;">
            <div class="chart-title">Trade Execution Log ({metrics['total_trades']} Trades)</div>
            <table id="tradeTable">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Entry Time</th>
                        <th>Exit Time</th>
                        <th>Side</th>
                        <th>ORB H1</th>
                        <th>ORB L1</th>
                        <th>Entry Price</th>
                        <th>Exit Price</th>
                        <th>SL</th>
                        <th>TP</th>
                        <th>Result</th>
                        <th>Gross PnL</th>
                        <th>Fees</th>
                        <th>Net PnL</th>
                        <th>Equity</th>
                    </tr>
                </thead>
                <tbody>
                    {trades_html}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        const ctxEquity = document.getElementById('equityChart').getContext('2d');
        new Chart(ctxEquity, {{
            type: 'line',
            data: {{
                labels: [{trade_labels}],
                datasets: [{{
                    label: 'Equity (USD)',
                    data: [{equity_points}],
                    borderColor: '#58a6ff',
                    backgroundColor: 'rgba(88, 166, 255, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.1,
                    pointRadius: 0
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ grid: {{ color: '#21262d' }} }},
                    y: {{ grid: {{ color: '#21262d' }} }}
                }}
            }}
        }});

        const ctxPnl = document.getElementById('pnlChart').getContext('2d');
        const pnlData = [{pnl_points}];
        new Chart(ctxPnl, {{
            type: 'bar',
            data: {{
                labels: pnlData.map((_, i) => i + 1),
                datasets: [{{
                    label: 'Net PnL ($)',
                    data: pnlData,
                    backgroundColor: pnlData.map(v => v >= 0 ? '#238636' : '#da3633')
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ grid: {{ display: false }} }},
                    y: {{ grid: {{ color: '#21262d' }} }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""

    out_path.write_text(html_content, encoding="utf-8")


def run_gold_orb_backtest(
    filepath: Path,
    initial_capital: float = 1000.0,
    leverage: int = 100,
    lot_size: int = 1000,
    fee_rate: float = 0.0001,
    rr_ratio: float = 1.25,
):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    logger = logging.getLogger(__name__)

    logger.info("Loading OHLCV data from: %s", filepath)
    df = load_ohlcv_data(filepath)
    logger.info("Loaded %d 1H candles", len(df))

    strategy = GoldOrbStrategy("XAUTUSD")
    strategy.fixed_lot_size = lot_size
    strategy.leverage = leverage
    strategy.rr_ratio = rr_ratio

    trades = strategy.run_backtest(df)
    logger.info("Executed %d trades", len(trades))


    if not trades:
        print("No trades generated.")
        return

    contract_val = 0.001
    taker_fee_rate = fee_rate  # 0.01% fee rate (0.0001) per user instruction

    equity = initial_capital
    peak_equity = initial_capital
    max_drawdown = 0.0

    trade_records = []
    wins = 0

    for t in trades:
        entry_p = t["entry_price"]
        exit_p = t["exit_price"]
        side = t["type"]

        gross_pnl = (exit_p - entry_p if side == "LONG" else entry_p - exit_p) * lot_size * contract_val
        notional_entry = entry_p * lot_size * contract_val
        notional_exit = exit_p * lot_size * contract_val
        fees = (notional_entry + notional_exit) * taker_fee_rate
        net_pnl = gross_pnl - fees

        equity += net_pnl
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100.0
        if dd > max_drawdown:
            max_drawdown = dd

        if net_pnl > 0:
            wins += 1

        rec = dict(t)
        rec["gross_pnl"] = round(gross_pnl, 2)
        rec["fees"] = round(fees, 2)
        rec["net_pnl"] = round(net_pnl, 2)
        rec["equity"] = round(equity, 2)
        trade_records.append(rec)

    tdf = pd.DataFrame(trade_records)
    wins_count = wins
    losses_count = len(tdf) - wins
    win_rate = (wins_count / len(tdf)) * 100.0 if len(tdf) > 0 else 0.0
    total_return_pct = ((equity - initial_capital) / initial_capital) * 100.0

    gross_profit = tdf[tdf["net_pnl"] > 0]["net_pnl"].sum()
    gross_loss = abs(tdf[tdf["net_pnl"] < 0]["net_pnl"].sum())
    total_fees = tdf["fees"].sum()
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    metrics = {
        "initial_capital": initial_capital,
        "final_capital": equity,
        "return_pct": total_return_pct,
        "total_trades": len(tdf),
        "wins": wins_count,
        "losses": losses_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "total_fees": total_fees,
        "start_date": tdf["entry_time"].iloc[0],
        "end_date": tdf["exit_time"].iloc[-1],
    }

    print("\n" + "=" * 65)
    print(f"  GOLD ORB BACKTEST RESULTS (XAUTUSD 1H, Fee: {fee_rate*100:.2f}%)")
    print("=" * 65)
    print(f"Period:              {metrics['start_date']} to {metrics['end_date']}")
    print(f"Initial Capital:     ${initial_capital:,.2f}")
    print(f"Final Equity:        ${equity:,.2f}")
    print(f"Total Return:        {total_return_pct:+.2f}%")
    print(f"Max Drawdown:        {max_drawdown:.2f}%")
    print(f"Total Trades:        {len(tdf)}")
    print(f"Win Rate:            {win_rate:.2f}% ({wins_count} W / {losses_count} L)")
    print(f"Profit Factor:       {profit_factor:.2f}")
    print(f"Gross Profit:        ${gross_profit:,.2f}")
    print(f"Gross Loss:          -${gross_loss:,.2f}")
    print(f"Total Fees:          ${total_fees:,.2f}")
    print("=" * 65 + "\n")

    out_dir = Path("backtest/reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save CSV
    out_csv = out_dir / "gold_orb_XAUTUSD_1h_trades.csv"
    tdf.to_csv(out_csv, index=False)
    logger.info("Trade log saved to: %s", out_csv.resolve())

    # Save HTML Report
    out_html = out_dir / "gold_orb_XAUTUSD_1h_report.html"
    generate_html_report(metrics, tdf, out_html)
    logger.info("HTML report saved to: %s", out_html.resolve())
    print(f"\n[OK] HTML Report generated: file:///{out_html.resolve()}\n")


def main():
    parser = argparse.ArgumentParser(description="Gold ORB Strategy Backtest")
    parser.add_argument("--file", type=str, default=str(DEFAULT_DATA_PATH), help="Path to XAUTUSD 1H CSV file")
    parser.add_argument("--capital", type=float, default=1000.0, help="Initial capital USD")
    parser.add_argument("--leverage", type=int, default=100, help="Leverage multiplier")
    parser.add_argument("--lot-size", type=int, default=1000, help="Fixed lot size (contracts)")
    parser.add_argument("--fee-rate", type=float, default=0.0001, help="Fee rate per trade (e.g. 0.0001 for 0.01%%)")
    parser.add_argument("--rr-ratio", type=float, default=1.25, help="Risk/Reward ratio multiplier (e.g. 1.25)")

    args = parser.parse_args()

    run_gold_orb_backtest(Path(args.file), args.capital, args.leverage, args.lot_size, args.fee_rate, args.rr_ratio)




if __name__ == "__main__":
    main()
