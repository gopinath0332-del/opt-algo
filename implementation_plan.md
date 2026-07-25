# Gold ORB (Opening Range Breakout) Strategy for XAUTUSD

## Overview

Implement a 1H Opening Range Breakout (ORB) strategy for XAUTUSD futures on Delta Exchange.

**Strategy Logic:**
- **Symbol:** XAUTUSD (Gold)
- **Timeframe:** 1H candles
- **Opening Range:** 06:30–07:30 AM IST (01:00–02:00 UTC) — one single 1H candle
- **H1** = High of the 01:00 UTC candle
- **L1** = Low of the 01:00 UTC candle
- **Long Entry:** When any subsequent candle **closes above H1** → LONG at close price
- **Short Entry:** When any subsequent candle **closes below L1** → SHORT at close price
- **Risk/Reward:** 1:1 — LONG: SL=L1, TP=Entry+(Entry−L1) | SHORT: SL=H1, TP=Entry−(H1−Entry)
- **TP/SL Execution (Live):** Single **OCO bracket order** placed immediately after entry fill — exchange monitors tick-by-tick and auto-cancels the other leg when one fires. **Bot does NOT poll for exits.**
- **TP/SL Execution (Backtest):** Checked per 1H candle using High/Low intra-bar simulation. SL wins on same-candle conflict.
- **Leverage:** 100x
- **Lot Size:** Fixed 1000 contracts
- **Max 1 trade per day** — resets at next 6:30 IST ORB candle
- **Firestore:** Journal entry + exit with full PnL details
- **Discord:** Notify on entry and on exit (TP hit or SL hit)

## Proposed Changes

---

### 1. New Strategy File

#### [NEW] [gold_orb_strategy.py](file:///d:/Workspace/delta-exchange-alog/strategies/gold_orb_strategy.py)

Full ORB strategy class extending `BaseStrategy`:

- **IST→UTC:** ORB window = 01:00 UTC (the 06:30 IST candle)
- **ORB detection:** Identify candle where `candle_time_utc.hour == 1 and candle_time_utc.minute == 0`
- **Entry:** First candle close after ORB candle that breaks H1 (LONG) or L1 (SHORT)
- **SL/TP calculation:**
  - LONG: `sl = L1`, `tp = entry + (entry - L1)`
  - SHORT: `sl = H1`, `tp = entry - (H1 - entry)`
- **Fixed lot size:** 1000 (ignores fractional sizing engine)
- **Daily reset:** `orb_h1`, `orb_l1`, `trade_taken_today` reset each new day
- **`run_backtest(df)`:** Full candle-loop simulation using High/Low for TP/SL detection
- **`check_signals()`:** Live signal detection on closed candle
- **`update_position_state()`:** Updates internal state, journals to Firestore, sends Discord

---

### 2. API Upgrade — Bracket Order with TP + SL (OCO)

#### [MODIFY] [rest_client.py](file:///d:/Workspace/delta-exchange-alog/api/rest_client.py)

Upgrade `place_bracket_order()` to accept and send **both** `take_profit_order` and `stop_loss_order`:

```python
# NEW payload structure (true OCO — exchange cancels the other leg automatically)
payload = {
    "product_id": product_id,
    "product_symbol": product_symbol,
    "take_profit_order": {
        "order_type": "limit_order",
        "limit_price": tp_price,          # Exact TP price
    },
    "stop_loss_order": {
        "order_type": "market_order",
        "stop_price": sl_price,           # SL trigger price
    },
    "bracket_stop_trigger_method": stop_trigger_method,
}
```

> Exchange fires whichever is hit first — **bot does nothing more after placing the bracket.**

---

### 3. Firestore Trade Journaling

Reuses existing [`journal_trade()`](file:///d:/Workspace/delta-exchange-alog/core/firestore_client.py#L75) in `core/firestore_client.py`.

**On Entry:**
```python
journal_trade(
    symbol="XAUTUSD", action="ENTRY_LONG", side="buy",
    price=entry_price, order_size=1000, leverage=100,
    trade_id=trade_id, strategy_name="Gold ORB",
    reason=f"ORB breakout above H1={orb_h1}",
    is_entry=True,
    orb_h1=orb_h1, orb_l1=orb_l1, tp_price=tp, sl_price=sl
)
```

**On Exit (position poll detects closure):**
```python
journal_trade(
    symbol="XAUTUSD", action="EXIT_LONG", side="sell",
    price=exit_price, pnl=realized_pnl,
    trade_id=trade_id, is_entry=False,
    reason="TP hit" or "SL hit",
    exit_price=exit_price, entry_price=entry_price
)
```

---

### 4. Discord Notifications

Reuses existing [`send_trade_alert()`](file:///d:/Workspace/delta-exchange-alog/notifications/discord.py#L70) in `notifications/discord.py`.

**On Entry — bot sends:**
```
🚀 TRADING SIGNAL: LONG ENTRY XAUTUSD (1h)
Strategy: Gold ORB
Price: $3250.00
Stop Loss: $3230.00
Take Profit: $3270.00
Lot Size: 1000 contracts
Reason: ORB breakout above H1=3245
```

**On Exit — bot sends:**
```
🚀 TRADING SIGNAL: EXIT LONG XAUTUSD (1h)
Strategy: Gold ORB
Price: $3270.00
P&L: +$20.00
Reason: TP hit at 3270
```

---

### 5. Settings Config Update

#### [MODIFY] [settings.yaml](file:///d:/Workspace/delta-exchange-alog/config/settings.yaml)

```yaml
strategies:
  gold_orb:
    trade_mode: "Both"
    orb_start_hour_utc: 1
    orb_start_minute_utc: 0
    fixed_lot_size: 1000

single_coin:
  XAUT:
    leverage: 100
    target_margin: 100
```

---

### 6. Backtest Runner Update

#### [MODIFY] [run_backtest.py](file:///d:/Workspace/delta-exchange-alog/run_backtest.py)

Add `gold-orb` to strategy factory and menu list.

---

## How Live Trading Works (Bot Lifecycle)

```
7:30 IST — ORB candle closes
   → Bot records H1, L1

8:30, 9:30... IST — Each 1H candle close
   → Check: close > H1? → LONG
   → Check: close < L1? → SHORT
   → Entry: place market order (1000 contracts, 100x)
   → Immediately after fill: place ONE bracket order (TP + SL)
   → Journal entry to Firestore
   → Send Discord entry alert
   → Bot's job is done for this trade

Exchange monitors tick-by-tick:
   → TP hit → fills TP, cancels SL automatically
   → SL hit → fills SL, cancels TP automatically

Next 1H candle poll (position check):
   → Bot sees position is FLAT
   → Fetches exit price + PnL from exchange
   → Journals exit to Firestore
   → Sends Discord exit alert
   → Sets trade_taken_today = True (no more entries today)
```

---

## Verification Plan

### Backtest Command
```bash
python run_backtest.py --strategy gold-orb --symbol XAUTUSD --timeframe 1h \
  --data-folder "D:\Workspace\crypto-backtest-data\Futures\delta_crypto_2026-01-01_to_2026-07-21_1h\delta_crypto_data" \
  --candle-type standard
```

### Manual Verification
- ORB candle correctly identified as 01:00 UTC
- TP and SL prices match 1:1 RR
- Bracket order payload contains both `take_profit_order` and `stop_loss_order`
- Firestore document created on entry, updated on exit with PnL
- Discord alert sent on entry and exit

