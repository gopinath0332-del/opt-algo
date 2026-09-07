"""
backtest/run_otm2_stoploss_comparison.py
=========================================
Tests OTM+2 Short Strangle at 13:00 IST with multiple stop-loss levels,
compared against the baseline Current Config (ATM Straddle, no SL).

Stop-loss fires when combined exit premium >= entry_premium × (1 + sl_pct/100):
  50%  SL → exit when premium expands to 1.5× entry  (tight)
  75%  SL → exit when premium expands to 1.75× entry
  100% SL → exit when premium expands to 2.0× entry  (doubles)
  No SL    → hold to expiry always

Data is loaded once. All engines run in a single pass per trading day.

Usage:
  python backtest/run_otm2_stoploss_comparison.py
  python backtest/run_otm2_stoploss_comparison.py --sl 50 75 100 150
  python backtest/run_otm2_stoploss_comparison.py --start 2025-01 --end 2026-06
  python backtest/run_otm2_stoploss_comparison.py --month 2025-06 --sl 50 100
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from pathlib import Path
from typing import List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, ShortStrangleEngine
from backtest.portfolio import Portfolio
from backtest.report import ReportGenerator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time constants
# ---------------------------------------------------------------------------
ENTRY_1700_UTC  = time(11, 30)   # 17:00 IST → current config
ENTRY_1300_UTC  = time(7,  30)   # 13:00 IST → OTM+2 strangle
EXPIRY_EXIT_UTC = time(12,  0)   # 17:30 IST → daily settlement

NO_SL = 9999.0   # effectively disabled


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all(
    start: str,
    end: str,
    capital: float,
    otm_steps: int,
    sl_levels: List[float],
) -> dict:
    """
    Load data once, run:
      - 1 baseline engine  : Current Config (ATM straddle, 17:00 IST, no SL)
      - 1 no-SL engine     : OTM+2 strangle, 13:00 IST, no SL
      - N SL engines       : OTM+2 strangle, 13:00 IST, sl_pct in sl_levels
    """
    base_kw = dict(
        start_month     = start,
        end_month       = end,
        initial_capital = capital,
        exit_time_utc   = EXPIRY_EXIT_UTC,
    )

    # Baseline: current config
    cfg_base = BacktestConfig(**base_kw, entry_time_utc=ENTRY_1700_UTC, sl_pct=NO_SL, otm_steps=0)

    # OTM+2 no SL
    cfg_nosl = BacktestConfig(**base_kw, entry_time_utc=ENTRY_1300_UTC, sl_pct=NO_SL, otm_steps=otm_steps)

    # OTM+2 with varying SL levels
    sl_cfgs = [
        BacktestConfig(**base_kw, entry_time_utc=ENTRY_1300_UTC, sl_pct=sl, otm_steps=otm_steps)
        for sl in sl_levels
    ]

    engine_base = ShortStraddleEngine(cfg_base)
    engine_nosl = ShortStrangleEngine(cfg_nosl)
    sl_engines  = [ShortStrangleEngine(cfg) for cfg in sl_cfgs]

    day_count = 0
    log.info("Running %d engines on all trading days...", 2 + len(sl_engines))

    for trade_date, day_df in iter_trading_days(cfg_base):
        day_count += 1
        engine_base.run_day(trade_date, day_df)
        engine_nosl.run_day(trade_date, day_df)
        for eng in sl_engines:
            eng.run_day(trade_date, day_df)

    log.info(
        "Done: %d days | Baseline: %d trades | NoSL: %d trades | SL engines: %s",
        day_count,
        len(engine_base.trades),
        len(engine_nosl.trades),
        [len(e.trades) for e in sl_engines],
    )

    port_base = Portfolio(cfg_base, engine_base.trades)
    port_nosl = Portfolio(cfg_nosl, engine_nosl.trades)
    sl_ports  = [Portfolio(cfg, eng.trades) for cfg, eng in zip(sl_cfgs, sl_engines)]

    return {
        "baseline":    (cfg_base, engine_base, port_base),
        "nosl":        (cfg_nosl, engine_nosl, port_nosl),
        "sl_variants": list(zip(sl_cfgs, sl_engines, sl_ports)),
        "sl_levels":   sl_levels,
        "otm_steps":   otm_steps,
        "day_count":   day_count,
    }


# ---------------------------------------------------------------------------
# Print comparison
# ---------------------------------------------------------------------------

def print_comparison(results: dict) -> None:
    sl_levels = results["sl_levels"]
    otm_steps = results["otm_steps"]
    otm_dist  = otm_steps * 200

    _, _, port_base = results["baseline"]
    _, _, port_nosl = results["nosl"]
    sl_ports        = [p for _, _, p in results["sl_variants"]]
    sl_engines      = [e for _, e, _ in results["sl_variants"]]

    b   = port_base.stats
    n   = port_nosl.stats
    sls = [p.stats for p in sl_ports]

    col  = 18
    ncol = 2 + len(sl_levels)   # baseline + nosl + N sl variants
    sep  = "=" * (34 + (col + 2) * ncol)
    thin = "-" * (34 + (col + 2) * ncol)

    def row(label, *vals):
        parts = [f"  {label:<32}"]
        for v in vals:
            parts.append(f"{str(v):>{col}}")
        print("  ".join(parts))

    # Header labels
    hdrs = (
        ["Current Config\n(ATM, No SL)", f"OTM+{otm_steps}\n(No SL)"] +
        [f"OTM+{otm_steps}\nSL {sl:.0f}%" for sl in sl_levels]
    )

    print()
    print(sep)
    print("  OTM+2 STRANGLE — STOP-LOSS SWEEP vs Current Config")
    print(sep)
    print(f"  Current Config  : 17:00 IST entry | ATM Straddle  | No SL | Expiry exit")
    print(f"  OTM+{otm_steps} variants : 13:00 IST entry | ±${otm_dist} Strangle | SL sweep | Expiry/SL exit")
    print(sep)

    # Column headers
    parts = [f"  {'Metric':<32}"]
    for h in hdrs:
        label = h.replace("\n", " ")
        parts.append(f"{label:>{col}}")
    print("  ".join(parts))
    print(thin)

    all_stats = [b, n] + sls

    row("Trades",
        *[s.get("total_trades", "--") for s in all_stats])
    row("SL Hits",
        *[s.get("sl_hit_count", 0) for s in all_stats])
    row("Skipped",
        *(len(e.skipped) for e in
          [results["baseline"][1], results["nosl"][1]] + [e for _, e, _ in results["sl_variants"]]))
    print(thin)

    row("Win Rate",
        *[f"{s.get('win_rate_pct', 0):.1f}%" for s in all_stats])
    row("Avg Daily P&L",
        *[f"${s.get('avg_pnl_per_trade', 0):+.2f}" for s in all_stats])
    row("Total Net P&L",
        *[f"${s.get('total_pnl_usd', 0):+,.2f}" for s in all_stats])
    row("Total Return %",
        *[f"{s.get('total_return_pct', 0):+.1f}%" for s in all_stats])
    row("Final Equity",
        *[f"${s.get('final_equity', 0):,.2f}" for s in all_stats])
    print(thin)

    row("Best Day",
        *[f"${s.get('max_win_usd', 0):+.2f}" for s in all_stats])
    row("Worst Day",
        *[f"${s.get('max_loss_usd', 0):+.2f}" for s in all_stats])
    row("Avg Win",
        *[f"${s.get('avg_win_usd', 0):+.2f}" for s in all_stats])
    row("Avg Loss",
        *[f"${s.get('avg_loss_usd', 0):+.2f}" for s in all_stats])
    row("Profit Factor",
        *[f"{s.get('profit_factor', 0):.2f}" for s in all_stats])
    print(thin)

    row("Max Drawdown ($)",
        *[f"${s.get('max_drawdown_usd', 0):+,.2f}" for s in all_stats])
    row("Max Drawdown (%)",
        *[f"{s.get('max_drawdown_pct', 0):.1f}%" for s in all_stats])
    row("Sharpe Ratio",
        *[f"{s.get('sharpe_ratio', 0):.2f}" for s in all_stats])
    row("Calmar Ratio",
        *[f"{s.get('calmar_ratio', 0):.2f}" for s in all_stats])
    print(thin)

    row("Avg Entry Premium",
        *[f"${s.get('avg_entry_premium', 0):.2f}" for s in all_stats])
    row("Avg Hold Time",
        *[f"{s.get('avg_hold_minutes', 0):.0f} min" for s in all_stats])
    row("Total Fees",
        *[f"${s.get('total_fee_usd', 0):.2f}" for s in all_stats])
    print(sep)

    # SL impact table
    print()
    print("  SL IMPACT vs OTM+2 No-SL baseline:")
    print(thin)
    print(f"  {'SL Level':<12}  {'Net P&L Δ':>14}  {'Win Rate Δ':>12}  {'Max DD Δ':>10}  {'Sharpe Δ':>10}  {'SL Hits':>8}  {'Avg Loss':>12}")
    print(f"  {'-'*12}  {'-'*14}  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*8}  {'-'*12}")
    for sl, s, eng in zip(sl_levels, sls, sl_engines):
        pnl_d  = s.get("total_pnl_usd", 0)  - n.get("total_pnl_usd", 0)
        wr_d   = s.get("win_rate_pct", 0)   - n.get("win_rate_pct", 0)
        dd_d   = s.get("max_drawdown_pct", 0) - n.get("max_drawdown_pct", 0)
        sh_d   = s.get("sharpe_ratio", 0)   - n.get("sharpe_ratio", 0)
        sl_hits= s.get("sl_hit_count", 0)
        avg_l  = s.get("avg_loss_usd", 0)
        print(
            f"  SL {sl:.0f}%        "
            f"  ${pnl_d:>+12,.2f}"
            f"  {wr_d:>+10.1f}%"
            f"  {dd_d:>+8.1f}%"
            f"  {sh_d:>+8.2f}"
            f"  {sl_hits:>8d}"
            f"  ${avg_l:>+10.2f}"
        )
    print(sep)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OTM+2 Strangle — Stop-Loss Sweep vs Current Config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",     default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",       default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",     default=None,      metavar="YYYY-MM",
                   help="Run a single month (overrides --start/--end)")
    p.add_argument("--otm-steps", type=int, default=2,
                   help="OTM strike steps from ATM (default: 2 = ±$400)")
    p.add_argument("--sl",        type=float, nargs="+", default=[50.0, 75.0, 100.0],
                   help="Stop-loss %% levels to test (default: 50 75 100). "
                        "SL fires when combined premium ≥ entry × (1 + sl/100).")
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
    sl_levels = sorted(args.sl)

    log.info("=" * 60)
    log.info("  OTM+%d Strangle — Stop-Loss Sweep", args.otm_steps)
    log.info("  Period     : %s → %s", start, end)
    log.info("  Capital    : $%s", f"{args.capital:,.0f}")
    log.info("  OTM dist   : ±$%d from ATM", args.otm_steps * 200)
    log.info("  SL levels  : %s", ", ".join(f"{s:.0f}%" for s in sl_levels))
    log.info("=" * 60)

    results = run_all(
        start     = start,
        end       = end,
        capital   = args.capital,
        otm_steps = args.otm_steps,
        sl_levels = sl_levels,
    )

    print_comparison(results)

    # Individual summaries
    _, _, port_base = results["baseline"]
    _, _, port_nosl = results["nosl"]

    print("---- A: Current Config (17:00 IST ATM Straddle, No SL) ----")
    port_base.print_summary()

    print(f"\n---- B: OTM+{args.otm_steps} Strangle (13:00 IST, No SL) ----")
    port_nosl.print_summary()

    for sl, (cfg, eng, port) in zip(sl_levels, results["sl_variants"]):
        print(f"\n---- OTM+{args.otm_steps} Strangle (13:00 IST, SL {sl:.0f}%) ----")
        port.print_summary()

    # HTML reports
    if not args.no_report:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        cfg_base, _, _ = results["baseline"]
        if port_base.trade_df is not None and not port_base.trade_df.empty:
            d = REPORTS_DIR / f"current_config_1700_atm_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_base, port_base).generate(d)
            log.info("Baseline report: %s", h)
            print(f"\n  Baseline (A) report        : {h}")

        cfg_nosl, _, _ = results["nosl"]
        if port_nosl.trade_df is not None and not port_nosl.trade_df.empty:
            d = REPORTS_DIR / f"otm{args.otm_steps}_1300_nosl_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_nosl, port_nosl).generate(d)
            log.info("OTM+2 no-SL report: %s", h)
            print(f"  OTM+2 no SL (B) report     : {h}")

        for sl, (cfg, eng, port) in zip(sl_levels, results["sl_variants"]):
            if port.trade_df is not None and not port.trade_df.empty:
                sl_tag = f"sl{sl:.0f}pct"
                d = REPORTS_DIR / f"otm{args.otm_steps}_1300_{sl_tag}_{start}_{end}_{ts}"
                h = ReportGenerator(cfg, port).generate(d)
                log.info("SL %.0f%% report: %s", sl, h)
                print(f"  OTM+2 SL {sl:.0f}% report         : {h}")
        print()


if __name__ == "__main__":
    main()
