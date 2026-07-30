# Short Straddle Strategy — Improvement Analysis

Based on a thorough review of the codebase (`strategy/short_straddle.py`, `config/settings.yaml`, `api/rest_client.py`, `main.py`), here are the improvements organized by impact and category.

---

## 1. Fee & Slippage Reduction (Highest P&L Impact)

### A. ✅ Hold to Expiry (Auto-Settlement) — Already Implemented
The code already supports this when `exit_time` is set to `"17:30"`. This saves ~50% of exit fees and 100% of exit slippage on legs that expire OTM.

### B. Limit Orders for Entry (Maker Rebates)
Currently the bot always uses `market_order` (0.03% taker fee). Switching to **limit orders at the bid/ask** would:
- Reduce fee to **0.02% maker** (or lower on Delta)
- Eliminate negative slippage on entry
- **Risk**: Partial fills or non-execution if the market moves quickly

**Implementation**: Add a `limit_entry` mode that:
1. Fetches the current best bid (for selling call/put)
2. Places a limit sell at the bid price
3. Waits up to N seconds for fill, with a fallback to market order if unfilled

```yaml
# settings.yaml
order_type: "limit_order"        # or "market_order"
limit_order_timeout_sec: 10      # fallback to market if unfilled
```

### C. Staggered Entry (Reduce Market Impact)
For large lot sizes (500+), splitting the entry into 2-3 smaller orders with 1-2s gaps can reduce slippage. Currently, the full lot is submitted as a single market order.

---

## 2. Entry Filters (Avoid Bad Days)

### A. Pre-Entry Momentum / Trend Filter
Short straddles get destroyed on high-momentum days. The `Requirement.md` already mentions this idea.

**Implementation**: Before entry at 17:00 IST, check if BTC has moved more than X% in the last N hours:

```python
# In _execute_entry(), before placing orders:
spot_now = self.client.get_spot_price(self.underlying)
spot_2h_ago = self.client.get_historical_price(self.underlying, hours_ago=2)
move_pct = abs(spot_now - spot_2h_ago) / spot_2h_ago * 100

if move_pct > self.strategy_config.momentum_filter_pct:  # e.g., 1.0%
    logger.warning(f"Momentum filter triggered: {move_pct:.2f}% move in 2h. Skipping trade.")
    self.notifier.send_status_message(...)
    return
```

```yaml
# settings.yaml
momentum_filter:
  enabled: true
  lookback_hours: 2
  max_move_pct: 1.0        # Skip if BTC moved > 1% in 2h
```

### B. Implied Volatility (IV) Filter
Skip entry when IV is abnormally low (premium not worth the risk) or abnormally high (expecting a large move):

```yaml
iv_filter:
  enabled: true
  min_iv_pct: 30            # Don't sell if IV is too low (premium too thin)
  max_iv_pct: 120           # Don't sell if IV is extremely high (crash risk)
```

### C. Day-of-Week Filter
Beyond just skipping weekends (`skip_weekends: false`), certain days may historically perform worse. Add an allowlist/blocklist:

```yaml
skip_days: []               # e.g., ["Friday"] to skip Fridays
```

---

## 3. Risk Management Improvements

### A. Per-Leg Stop-Loss (Not Just Combined)
Currently, the SL is based on **combined** MTM loss. If one leg is deep ITM but the other decays, the combined loss might not hit the threshold until it's too late. Add an **individual leg SL**:

```yaml
stop_loss:
  type: "premium_pct"
  value: 50                  # Combined SL (existing)
  per_leg_enabled: true
  per_leg_value: 100         # Close individual leg if it doubles in price
```

In `_monitor_stop_loss()`:
```python
# Check per-leg SL
call_move = current_call_premium / self.call_entry_premium
put_move = current_put_premium / self.put_entry_premium

if call_move > self.per_leg_sl_multiplier:
    # Close only the call leg, hold put
    ...
```

### B. Trailing Stop-Loss
Once the position is profitable (premiums decaying in your favor), trail the SL upward to lock in gains:

```yaml
stop_loss:
  trailing_enabled: true
  trailing_trigger_pct: 30   # Activate trailing after 30% of premium decayed
  trailing_distance_pct: 20  # Trail SL at 20% behind peak profit
```

### C. Max Daily Loss Circuit Breaker
If running multiple straddles (BTC + XAUT), a daily loss cap prevents compounding losses:

```yaml
risk:
  max_daily_loss_usd: 500    # Stop all strategies if daily loss exceeds $500
```

### D. Position Size Scaling Based on IV
When IV is higher, premium collected is larger but risk is also higher. Scale lot size inversely with IV to maintain consistent risk:

```python
# Reduce lot size when IV is elevated
if current_iv > baseline_iv:
    scale_factor = baseline_iv / current_iv  # e.g., 0.7x for elevated IV
    self.lot_size = int(self.lot_size * scale_factor)
```

---

## 4. Execution & Monitoring Improvements

### A. WebSocket-Based Monitoring (Replace Polling)
Currently, `_monitor_stop_loss()` polls every `monitor_interval_sec` (60s). During that 60s window, the SL could be breached significantly.

**Improvement**: Use Delta Exchange WebSocket for real-time mark price streaming:
- Near-instant SL detection (sub-second vs 60s)
- Reduces API rate limit usage
- Detects flash crashes that polling misses

### B. Server-Side Stop-Loss (Bracket Orders)
Instead of monitoring SL in the bot, place a **bracket order with SL** on the exchange itself:
- Exchange monitors tick-by-tick
- Works even if bot crashes or loses connectivity
- Bracket order logic already exists in the Gold ORB strategy

> **⚠️ WARNING**: This is the single most impactful reliability improvement. If the bot crashes during monitoring, there is currently **no SL protection** until the bot restarts and the recovery logic kicks in.

### C. Faster Monitor Interval
If WebSocket is too complex, simply reducing `monitor_interval_sec` from 60 to 5-10 seconds would significantly reduce SL breach overshoot. The current 60s interval is very coarse for a 30-minute strategy window.

### D. Async/Parallel Leg Execution
Currently, call and put orders are placed sequentially. Between the two orders, the market can move. Place both legs **concurrently** (async or threading):

```python
import concurrent.futures

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    call_future = executor.submit(self.client.place_order, self.call_product_id, ...)
    put_future = executor.submit(self.client.place_order, self.put_product_id, ...)
    call_order = call_future.result()
    put_order = put_future.result()
```

---

## 5. Strategy Variants (Alpha Generation)

### A. Short Strangle Instead of Straddle
Sell OTM Call + OTM Put (e.g., 1-2 strikes away from ATM) instead of ATM. This gives:
- Wider profit range (lower probability of SL)
- Lower premium collected (smaller absolute P&L per trade)
- Better win rate

```yaml
strategy_type: "strangle"    # or "straddle"
otm_offset_strikes: 1        # Number of strikes OTM for each leg
```

### B. Iron Butterfly / Iron Condor
Add protective long legs to cap max loss. Buy further OTM options as hedges:
- Short ATM Call + Put (collect premium)
- Long OTM Call + Put (cap loss)
- **Defined risk** — no margin call surprises

### C. Dynamic Strike Selection
Instead of always selling ATM, use delta-based selection:
- Sell the 0.50-delta call and 0.50-delta put (true ATM by delta)
- Or sell 0.40-delta for slightly OTM positioning on high-IV days

### D. Multi-Expiry (Calendar Spread)
Sell the daily expiry and buy a weekly/monthly expiry as a hedge — profits from time decay differential.

---

## 6. Analytics & Observability

### A. Detailed Trade Metrics in Firestore
Currently tracked: entry/exit premiums, P&L, slippage, fees. Add:
- **Greeks at entry**: Delta, Gamma, Theta, Vega (if available from the API)
- **IV at entry vs realized volatility**: helps post-hoc analysis of which IV regime is most profitable
- **Time in trade**: seconds from entry to SL/expiry
- **Max favorable excursion (MFE)**: peak unrealized profit during the trade

### B. Performance Dashboard
Build a simple dashboard (or Firestore → Google Sheets sync) that shows:
- Daily P&L curve
- Win rate by day of week
- Average premium collected vs average loss
- Slippage as % of premium
- Fee drag as % of gross P&L

### C. Discord Notification Enhancements
- Add **running P&L** (daily/weekly/monthly totals) to exit alerts
- Add **win streak / loss streak** counter
- Add **account balance** to exit alerts for quick health check

---

## 7. Code Quality & Reliability

### A. Reduce Code Duplication in Fill Fetching
The entry fill fetching and exit fill fetching blocks are nearly identical (~90 lines each). Extract into a shared helper:

```python
def _fetch_fill_price(self, order_id: int, product_id: int, mark_price: float, label: str) -> float:
    """Fetch actual fill price with fallback chain: fills → order → mark."""
    ...
```

### B. Graceful Shutdown Handler
Add a `SIGTERM` / `SIGINT` handler that:
1. Cancels any pending orders
2. Closes open positions
3. Journals the emergency exit to Firestore
4. Sends a Discord alert

Currently, a `Ctrl+C` during monitoring leaves positions open with no alerting.

### C. Health Check Endpoint
Add a simple HTTP health check (e.g., Flask on port 8080) for monitoring the bot's liveness from external tools (Uptime Robot, etc.).

---

## Priority Ranking

| # | Improvement | Impact | Effort | Risk |
|---|---|---|---|---|
| 1 | **Server-side SL (bracket orders)** | 🔴 Critical | Medium | Low |
| 2 | **Faster monitor interval (60s → 5s)** | 🟠 High | Trivial | None |
| 3 | **Pre-entry momentum filter** | 🟠 High | Low | Low |
| 4 | **Limit orders for entry** | 🟡 Medium | Medium | Medium |
| 5 | **Parallel leg execution** | 🟡 Medium | Low | Low |
| 6 | **Per-leg stop-loss** | 🟡 Medium | Medium | Low |
| 7 | **Trailing stop-loss** | 🟡 Medium | Medium | Medium |
| 8 | **WebSocket monitoring** | 🟠 High | High | Medium |
| 9 | **Short strangle variant** | 🟡 Medium | Medium | Low |
| 10 | **Fill-fetch code dedup** | 🟢 Low | Low | None |
| 11 | **IV filter** | 🟡 Medium | Medium | Low |
| 12 | **Detailed trade metrics** | 🟢 Low | Low | None |

> **💡 TIP**: **Quick wins** (items 2, 5, 10) can be done in under an hour each. **Item 1** (server-side SL) is the most important for production reliability — the bot currently has no SL protection during crashes/restarts.
