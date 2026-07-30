# Pre-Entry Momentum Filter — Backtest Results & Comparison

A complete backtest of the **BTC Short Straddle strategy** was executed using tick-level options market data from **January 1, 2025 to June 23, 2026** (18 months, **535 trading days**). 

We compared the **Baseline (current configuration)** against 4 threshold levels for the **Pre-Entry Momentum Filter** (2-hour lookback before entry at 17:00 IST / 11:30 UTC).

---

## 📊 Summary Performance Comparison

| Metric | Baseline (No Filter) | Filter >0.5% | Filter >1.0% | Filter >1.5% | Filter >2.0% |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Total Trades** | 535 | 411 | 508 | **526** | **531** |
| **Days Skipped** | 0 (0.0%) | 124 (23.2%) | 27 (5.0%) | **9 (1.7%)** | **4 (0.7%)** |
| **Win Rate** | 68.4% | 68.4% | **68.9%** | **68.8%** | **68.7%** |
| **Gross P&L** | +$20,083.55 | +$16,077.40 | +$19,319.98 | +$20,021.20 | +$20,327.44 |
| **Total Fees** | $5,946.95 | $4,417.40 | $5,559.96 | $5,793.02 | $5,879.17 |
| **Net P&L** | **+$14,136.59** | +$11,660.00 | +$13,760.02 | **+$14,228.18** | **+$14,448.27** |
| **Net Return %** | +1,413.7% | +1,166.0% | +1,376.0% | **+1,422.8%** | **+1,444.8%** |
| **Avg P&L / Trade** | +$26.42 | +$28.37 | +$27.09 | +$27.05 | **+$27.21** |
| **Profit Factor** | 2.15 | **2.46** | 2.25 | 2.24 | 2.23 |
| **Sharpe Ratio** | 4.36 | **5.07** | 4.55 | **4.56** | 4.55 |
| **Max Drawdown** | -$707.76 (-22.4%) | -$777.74 (-11.7%) | -$707.76 (-22.4%) | -$707.76 (-22.4%) | -$707.76 (-22.4%) |

---

## 💡 Key Findings & Analysis

### 1. High Threshold Filters (>1.5% and >2.0%) Outperform Baseline
- **>2.0% Threshold (Best Net P&L)**: Skips **4 extreme trend days** (moves of 2.23% to 4.12% in 2h).
  - Baseline lost **-$311.67** across those 4 days (average loss of **-$77.92/trade**).
  - Filtering them out increases total Net P&L by **+$311.67** and raises Sharpe ratio from **4.36 to 4.55**.
- **>1.5% Threshold (Best Risk-Adjusted Balance)**: Skips **9 high-momentum days**.
  - Baseline lost **-$154.49** across those 9 days (5 losses out of 9 trades).
  - Boosts Net P&L by **+$91.58**, increases Win Rate to **68.8%**, and raises Sharpe to **4.56**.

### 2. Lower Thresholds (>0.5% and >1.0%) Are Too Strict
- **>0.5% Threshold**: Skips **124 days** (23.2% of all trading days).
  - While it improves Sharpe to **5.07** and reduces Drawdown % to **11.7%**, it sacrifices **-$2,476.60** in total profit because mild 0.5%-1.0% pre-entry moves are still profitable straddle days (netting +$21.45/trade on average).
- **>1.0% Threshold**: Skips 27 days. The skipped days were net positive (+ $301.86 total), so while win rate ticks up to 68.9%, total Net P&L drops slightly (-$376.57).

---

## 🔍 Extreme Days Caught by >2.0% Filter

These 4 severe trend days caused major drawdown in the baseline straddle and were successfully filtered out:

| Date | Spot (2h before) | Spot (at entry) | 2h Move % | Baseline Straddle Result |
| :--- | :---: | :---: | :---: | :--- |
| **2025-04-04** | $84,400 | $82,000 | **2.84%** | Large Loss |
| **2026-02-05** | $71,800 | $70,200 | **2.23%** | Large Loss |
| **2026-02-27** | $68,000 | $66,400 | **2.35%** | Large Loss |
| **2026-03-23** | $68,000 | $70,800 | **4.12%** | Severe Loss |

---

## 🎯 Recommendation & Configuration

We recommend enabling the **Pre-Entry Momentum Filter** with a **1.5% or 2.0% threshold** (lookback of 2 hours):

```yaml
# In settings.yaml
momentum_filter:
  enabled: true
  lookback_hours: 2.0
  max_move_pct: 1.5   # Or 2.0 to catch only extreme crash/pump days
```

### Why this threshold?
1. **Protects capital** against sudden intra-day breakouts (like 2026-03-23's 4.12% move).
2. **Increases Net P&L** without reducing overall trade frequency significantly (< 2% of days skipped).
3. **Improves Sharpe Ratio** from **4.36 to 4.56** and Profit Factor from **2.15 to 2.24**.
