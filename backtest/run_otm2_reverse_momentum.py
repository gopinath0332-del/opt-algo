"""
backtest/run_otm2_reverse_momentum.py
=======================================
"Reverse momentum filter": Enter the OTM+2 Strangle ONLY on high-momentum days
(i.e., when BTC moved MORE than the threshold in the 2h window before entry).

This is the opposite of the standard momentum filter which skips volatile days.
The hypothesis: OTM+2 strangle profits most on days where BTC makes a big move
BEFORE entry but then consolidates into expiry.

Tested configurations:
  A  — Current Config     : 17:00 IST, ATM Straddle, No SL, All days
  B  — OTM+2 All Days     : 13:00 IST, ±$400, No SL, All days
  C  — OTM+2 High-Mom     : 13:00 IST, ±$400, No SL, ONLY high-momentum days
  D  — OTM+2 High-Mom+SL  : 13:00 IST, ±$400, SL 150%, ONLY high-momentum days

Each of C and D is run across multiple thresholds for a sweep.

Momentum check: |spot_entry - spot_2h_ago| / spot_2h_ago > threshold
  → If true → ENTER trade (reverse of normal filter)
  → If false → SKIP

Usage:
  python backtest/run_otm2_reverse_momentum.py
  python backtest/run_otm2_reverse_momentum.py --thresholds 0.5 0.8 1.0 1.2 1.5 2.0
  python backtest/run_otm2_reverse_momentum.py --start 2025-01 --end 2026-06 --sl 150
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.config import BacktestConfig, REPORTS_DIR
from backtest.data_loader import iter_trading_days
from backtest.strategy import ShortStraddleEngine, ShortStrangleEngine
from backtest.portfolio import Portfolio
from backtest.price_engine import find_atm_strike
from backtest.report import ReportGenerator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time constants
# ---------------------------------------------------------------------------
ENTRY_1700_UTC  = time(11, 30)   # 17:00 IST — current config
ENTRY_1300_UTC  = time(7,  30)   # 13:00 IST — OTM+2 entry
EXPIRY_EXIT_UTC = time(12,  0)   # 17:30 IST — daily settlement
NO_SL = 9999.0


# ---------------------------------------------------------------------------
# Momentum helper (reused from run_otm2_momentum_comparison.py)
# ---------------------------------------------------------------------------

def should_skip_momentum(
    day_df,
    trade_date: date,
    entry_time_utc: time,
    lookback_hours: float,
    threshold_pct: float,
    window_minutes: int = 10,
) -> Tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """Standard momentum skip check. Returns (skip, spot_lb, spot_entry, move_pct)."""
    entry_dt    = datetime.combine(trade_date, entry_time_utc)
    lookback_dt = entry_dt - timedelta(hours=lookback_hours)

    if lookback_dt.date() != trade_date:
        return False, None, None, None

    lookback_time = lookback_dt.time()
    spot_lb = find_atm_strike(day_df, trade_date, lookback_time, window_minutes)
    spot_en = find_atm_strike(day_df, trade_date, entry_time_utc, window_minutes)

    if spot_lb is None or spot_en is None or spot_lb == 0:
        return False, spot_lb, spot_en, None

    move_pct = abs(spot_en - spot_lb) / spot_lb * 100.0
    skip     = move_pct > threshold_pct
    return skip, spot_lb, spot_en, move_pct


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all(
    start: str,
    end: str,
    capital: float,
    otm_steps: int,
    thresholds: List[float],
    sl_pct: float,
    lookback_hours: float,
) -> dict:
    """
    Single-pass through all trading days.
    Runs:
      - Baseline (A): ATM straddle, 17:00 IST, no SL
      - All-days (B): OTM+N strangle, 13:00 IST, no SL
      - For each threshold:
          C — reverse filter, no SL  (enter ONLY when |move| > threshold)
          D — reverse filter, SL%    (enter ONLY when |move| > threshold, exit at SL)
    """
    base_kw = dict(start_month=start, end_month=end,
                   initial_capital=capital, exit_time_utc=EXPIRY_EXIT_UTC)

    cfg_a    = BacktestConfig(**base_kw, entry_time_utc=ENTRY_1700_UTC,
                               sl_pct=NO_SL, otm_steps=0)
    cfg_b    = BacktestConfig(**base_kw, entry_time_utc=ENTRY_1300_UTC,
                               sl_pct=NO_SL, otm_steps=otm_steps)

    # One (no-SL, SL) pair per threshold
    cfg_pairs = [
        (
            BacktestConfig(**base_kw, entry_time_utc=ENTRY_1300_UTC,
                           sl_pct=NO_SL, otm_steps=otm_steps),
            BacktestConfig(**base_kw, entry_time_utc=ENTRY_1300_UTC,
                           sl_pct=sl_pct, otm_steps=otm_steps),
        )
        for _ in thresholds
    ]

    engine_a  = ShortStraddleEngine(cfg_a)
    engine_b  = ShortStrangleEngine(cfg_b)
    eng_pairs = [
        (ShortStrangleEngine(c_nosl), ShortStrangleEngine(c_sl))
        for c_nosl, c_sl in cfg_pairs
    ]

    # Momentum check stats per threshold
    mom_stats = [
        {"entered": 0, "skipped": 0, "details": []}
        for _ in thresholds
    ]

    log.info("Single-pass through trading days for %d engines...",
             2 + 2 * len(thresholds))
    day_count = 0

    for trade_date, day_df in iter_trading_days(cfg_a):
        day_count += 1

        # A — always
        engine_a.run_day(trade_date, day_df)
        # B — always
        engine_b.run_day(trade_date, day_df)

        # Per-threshold reverse filter
        for i, (thr, (eng_nosl, eng_sl), ms) in enumerate(
                zip(thresholds, eng_pairs, mom_stats)):

            is_skip, spot_lb, spot_en, move_pct = should_skip_momentum(
                day_df, trade_date, ENTRY_1300_UTC, lookback_hours, thr,
            )

            # Reverse: is_skip=True means |move| > threshold → ENTER
            if is_skip:
                ms["entered"] += 1
                ms["details"].append({
                    "date": trade_date,
                    "spot_lb": spot_lb,
                    "spot_en": spot_en,
                    "move_pct": move_pct,
                })
                eng_nosl.run_day(trade_date, day_df)
                eng_sl.run_day(trade_date, day_df)
                log.debug("%s | ENTER (%.2f%% > %.1f%%) | lb=%g en=%g",
                          trade_date, move_pct, thr, spot_lb or 0, spot_en or 0)
            else:
                # is_skip=False → |move| ≤ threshold → skip (reverse filter)
                ms["skipped"] += 1
                reason = (
                    f"reverse-momentum: {move_pct:.2f}% ≤ {thr:.1f}% threshold"
                    if move_pct is not None
                    else f"reverse-momentum: could not compute move vs {thr:.1f}%"
                )
                eng_nosl._skip(trade_date, reason)
                eng_sl._skip(trade_date, reason)

    log.info(
        "Done: %d days | A=%d | B=%d | per-threshold entered=%s",
        day_count,
        len(engine_a.trades), len(engine_b.trades),
        [ms["entered"] for ms in mom_stats],
    )

    port_a = Portfolio(cfg_a, engine_a.trades)
    port_b = Portfolio(cfg_b, engine_b.trades)
    port_pairs = [
        (Portfolio(c_nosl, eng_nosl.trades), Portfolio(c_sl, eng_sl.trades))
        for (c_nosl, c_sl), (eng_nosl, eng_sl) in zip(cfg_pairs, eng_pairs)
    ]

    return {
        "day_count":  day_count,
        "otm_steps":  otm_steps,
        "thresholds": thresholds,
        "sl_pct":     sl_pct,
        "baseline":   (cfg_a, engine_a, port_a),
        "alldays":    (cfg_b, engine_b, port_b),
        "variants":   list(zip(cfg_pairs, eng_pairs, port_pairs, mom_stats)),
    }


# ---------------------------------------------------------------------------
# Print comparison
# ---------------------------------------------------------------------------

def _fmt_stat(s: dict, key: str, fmt: str) -> str:
    v = s.get(key, 0)
    try:
        return fmt.format(v)
    except Exception:
        return str(v)


def print_comparison(results: dict) -> None:
    thresholds = results["thresholds"]
    otm_steps  = results["otm_steps"]
    otm_dist   = otm_steps * 200
    sl_pct     = results["sl_pct"]
    day_count  = results["day_count"]

    _, _, port_a = results["baseline"]
    _, _, port_b = results["alldays"]
    a = port_a.stats
    b = port_b.stats

    print()
    print("=" * 110)
    print("  OTM+2 REVERSE MOMENTUM FILTER — Enter ONLY on High-Momentum Days")
    print("=" * 110)
    print(f"  A : Current Config  — 17:00 IST, ATM Straddle, No SL, All {day_count} days")
    print(f"  B : OTM+{otm_steps} All Days  — 13:00 IST, ±${otm_dist}, No SL, All {day_count} days")
    print(f"  C : OTM+{otm_steps} Rev-Mom   — 13:00 IST, ±${otm_dist}, No SL, ONLY when |move| > threshold")
    print(f"  D : OTM+{otm_steps} Rev-Mom+SL— 13:00 IST, ±${otm_dist}, SL {sl_pct:.0f}%, ONLY when |move| > threshold")
    print("=" * 110)

    # --- Threshold sweep table (one row per threshold) ---
    col = 14
    print(f"\n  {'Threshold':<12}  {'Days In':>{col}}  {'Days Out':>{col}}  "
          f"{'C Net P&L':>{col}}  {'C WinR':>8}  {'D Net P&L':>{col}}  {'D WinR':>8}  {'D MaxDD':>10}  {'D Calmar':>10}")
    print("  " + "-" * 108)

    for i, (_, _, (port_nosl, port_sl), ms) in enumerate(results["variants"]):
        thr = thresholds[i]
        ns  = port_nosl.stats
        ss  = port_sl.stats
        print(
            f"  >{thr:<8.1f}%"
            f"  {ms['entered']:>{col}}"
            f"  {ms['skipped']:>{col}}"
            f"  ${ns.get('total_pnl_usd', 0):>+{col-2},.2f}"
            f"  {ns.get('win_rate_pct', 0):>8.1f}%"
            f"  ${ss.get('total_pnl_usd', 0):>+{col-2},.2f}"
            f"  {ss.get('win_rate_pct', 0):>8.1f}%"
            f"  {ss.get('max_drawdown_pct', 0):>10.1f}%"
            f"  {ss.get('calmar_ratio', 0):>10.2f}"
        )

    print("=" * 110)

    # --- Best threshold detail ---
    best_nosl_idx = max(range(len(thresholds)),
                        key=lambda i: results["variants"][i][2][0].stats.get("total_pnl_usd", 0))
    best_sl_idx   = max(range(len(thresholds)),
                        key=lambda i: results["variants"][i][2][1].stats.get("total_pnl_usd", 0))

    best_nosl_thr = thresholds[best_nosl_idx]
    best_sl_thr   = thresholds[best_sl_idx]
    _, _, (best_nosl_port, _), best_nosl_ms = results["variants"][best_nosl_idx]
    _, _, (_, best_sl_port),  best_sl_ms    = results["variants"][best_sl_idx]

    print()
    print("=" * 110)
    print(f"  FULL DETAIL: A vs B vs Best Rev-Filter (C: >{best_nosl_thr:.1f}% no SL, D: >{best_sl_thr:.1f}% SL{sl_pct:.0f}%)")
    print("=" * 110)

    cn = best_nosl_port.stats
    cs = best_sl_port.stats
    col2 = 22

    def row(label, *vals):
        parts = [f"  {label:<30}"]
        for v in vals:
            parts.append(f"{str(v):>{col2}}")
        print("  ".join(parts))

    hdrs = [
        "A: Current Config",
        "B: OTM+2 All Days",
        f"C: Rev>{best_nosl_thr:.1f}% No SL ({best_nosl_ms['entered']}t)",
        f"D: Rev>{best_sl_thr:.1f}% SL{sl_pct:.0f}% ({best_sl_ms['entered']}t)",
    ]
    print("  " + "  ".join(f"{h:>{col2}}" for h in ["Metric"] + hdrs))
    thin = "  " + "-" * (30 + (col2 + 2) * 4)
    print(thin)

    all_s = [a, b, cn, cs]

    row("Trades",         *[s.get("total_trades", 0) for s in all_s])
    row("SL Hits",        *[s.get("sl_hit_count", 0) for s in all_s])
    print(thin)
    row("Win Rate",       *[f"{s.get('win_rate_pct',0):.1f}%" for s in all_s])
    row("Avg Daily P&L",  *[f"${s.get('avg_pnl_per_trade',0):+.2f}" for s in all_s])
    row("Total Net P&L",  *[f"${s.get('total_pnl_usd',0):+,.2f}" for s in all_s])
    row("Total Return",   *[f"{s.get('total_return_pct',0):+.1f}%" for s in all_s])
    row("Final Equity",   *[f"${s.get('final_equity',0):,.2f}" for s in all_s])
    print(thin)
    row("Best Day",       *[f"${s.get('max_win_usd',0):+.2f}" for s in all_s])
    row("Worst Day",      *[f"${s.get('max_loss_usd',0):+.2f}" for s in all_s])
    row("Avg Win",        *[f"${s.get('avg_win_usd',0):+.2f}" for s in all_s])
    row("Avg Loss",       *[f"${s.get('avg_loss_usd',0):+.2f}" for s in all_s])
    row("Profit Factor",  *[f"{s.get('profit_factor',0):.2f}" for s in all_s])
    print(thin)
    row("Max Drawdown $", *[f"${s.get('max_drawdown_usd',0):+,.2f}" for s in all_s])
    row("Max Drawdown %", *[f"{s.get('max_drawdown_pct',0):.1f}%" for s in all_s])
    row("Sharpe Ratio",   *[f"{s.get('sharpe_ratio',0):.2f}" for s in all_s])
    row("Calmar Ratio",   *[f"{s.get('calmar_ratio',0):.2f}" for s in all_s])
    print(thin)
    row("Avg Entry Prem", *[f"${s.get('avg_entry_premium',0):.2f}" for s in all_s])
    row("Avg Hold Time",  *[f"{s.get('avg_hold_minutes',0):.0f} min" for s in all_s])
    row("Total Fees",     *[f"${s.get('total_fee_usd',0):.2f}" for s in all_s])
    print("=" * 110)

    # Per-day detail for best no-SL threshold
    print(f"\n  Days entered by reverse filter (>{best_nosl_thr:.1f}% move in 2h before 13:00 IST):")
    print(f"  {'Date':>12}  {'Spot (lookback)':>16}  {'Spot (entry)':>13}  {'Move %':>8}")
    print(f"  {'-'*12}  {'-'*16}  {'-'*13}  {'-'*8}")
    for d in best_nosl_ms["details"]:
        print(
            f"  {str(d['date']):>12}  "
            f"${d['spot_lb']:>14,.0f}  "
            f"${d['spot_en']:>11,.0f}  "
            f"{d['move_pct']:>7.2f}%"
        )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OTM+2 Reverse Momentum Filter — Enter only on high-momentum days",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",      default="2025-01", metavar="YYYY-MM")
    p.add_argument("--end",        default="2026-06", metavar="YYYY-MM")
    p.add_argument("--month",      default=None,      metavar="YYYY-MM")
    p.add_argument("--otm-steps",  type=int,   default=2)
    p.add_argument("--thresholds", type=float, nargs="+",
                   default=[0.5, 0.8, 1.0, 1.2, 1.5, 2.0],
                   help="Momentum thresholds to sweep (default: 0.5 0.8 1.0 1.2 1.5 2.0)")
    p.add_argument("--lookback",   type=float, default=2.0,
                   help="Lookback hours before entry (default: 2.0)")
    p.add_argument("--sl",         type=float, default=150.0,
                   help="SL %% for the D variants (default: 150)")
    p.add_argument("--capital",    type=float, default=1_000.0)
    p.add_argument("--no-report",  action="store_true")
    p.add_argument("--verbose",    action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    start      = args.month if args.month else args.start
    end        = args.month if args.month else args.end
    thresholds = sorted(args.thresholds)

    log.info("=" * 60)
    log.info("  OTM+%d Reverse Momentum Filter Sweep", args.otm_steps)
    log.info("  Period      : %s → %s", start, end)
    log.info("  Capital     : $%s", f"{args.capital:,.0f}")
    log.info("  OTM dist    : ±$%d", args.otm_steps * 200)
    log.info("  Thresholds  : %s", ", ".join(f"{t:.1f}%" for t in thresholds))
    log.info("  SL (D vars) : %.0f%%", args.sl)
    log.info("  Lookback    : %.1fh before 13:00 IST entry", args.lookback)
    log.info("=" * 60)

    results = run_all(
        start         = start,
        end           = end,
        capital       = args.capital,
        otm_steps     = args.otm_steps,
        thresholds    = thresholds,
        sl_pct        = args.sl,
        lookback_hours= args.lookback,
    )

    print_comparison(results)

    # Full summaries
    _, _, port_a = results["baseline"]
    _, _, port_b = results["alldays"]
    print("---- A: Current Config ----")
    port_a.print_summary()
    print("\n---- B: OTM+2 All Days (No SL) ----")
    port_b.print_summary()
    for i, (_, _, (port_nosl, port_sl), ms) in enumerate(results["variants"]):
        thr = thresholds[i]
        print(f"\n---- C: Rev >{thr:.1f}% No SL ({ms['entered']} trades) ----")
        port_nosl.print_summary()
        print(f"\n---- D: Rev >{thr:.1f}% SL {args.sl:.0f}% ({ms['entered']} trades) ----")
        port_sl.print_summary()

    # HTML reports for best variants only (to keep things tidy)
    if not args.no_report:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cfg_a, _, _ = results["baseline"]
        cfg_b, _, _ = results["alldays"]

        if port_a.trade_df is not None and not port_a.trade_df.empty:
            d = REPORTS_DIR / f"current_config_1700_atm_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_a, port_a).generate(d)
            print(f"\n  A report : {h}")

        if port_b.trade_df is not None and not port_b.trade_df.empty:
            d = REPORTS_DIR / f"otm{args.otm_steps}_alldays_nosl_{start}_{end}_{ts}"
            h = ReportGenerator(cfg_b, port_b).generate(d)
            print(f"  B report : {h}")

        for i, ((c_nosl, c_sl), _, (port_nosl, port_sl), ms) in enumerate(results["variants"]):
            thr = thresholds[i]
            thr_tag = f"{thr:.1f}".replace(".", "p")
            if port_nosl.trade_df is not None and not port_nosl.trade_df.empty:
                d = REPORTS_DIR / f"otm{args.otm_steps}_revmom{thr_tag}pct_nosl_{start}_{end}_{ts}"
                h = ReportGenerator(c_nosl, port_nosl).generate(d)
                print(f"  C >{thr:.1f}% report : {h}")
            if port_sl.trade_df is not None and not port_sl.trade_df.empty:
                d = REPORTS_DIR / f"otm{args.otm_steps}_revmom{thr_tag}pct_sl{args.sl:.0f}_{start}_{end}_{ts}"
                h = ReportGenerator(c_sl, port_sl).generate(d)
                print(f"  D >{thr:.1f}% SL{args.sl:.0f}% report : {h}")
        print()


if __name__ == "__main__":
    main()
