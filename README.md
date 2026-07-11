# Delta Exchange Options — Short Straddle Bot

Automated options trading bot for Delta Exchange that executes a **daily Short Straddle** strategy on BTC.

## Strategy

| Parameter | Value |
|---|---|
| **Strategy** | Short Straddle (sell ATM Call + ATM Put) |
| **Underlying** | BTC (configurable in `settings.yaml`) |
| **Entry Time** | 17:00 IST daily |
| **Exit Time** | 17:25 IST daily |
| **Lot Size** | 250 contracts per leg |
| **Leverage** | 200x |
| **Order Type** | Market |
| **Stop-Loss** | Combined — 50% of entry premium collected |

## How It Works

1. **17:00 IST** — Bot fetches BTC spot price, finds the ATM strike, and sells Call + Put at market
2. **17:00–17:25** — Monitors combined MTM P&L every 5 seconds against stop-loss threshold
3. **If SL hit** — Immediately closes both legs and sends Discord alert
4. **17:25 IST** — If SL wasn't hit, closes both legs at market and sends exit alert

## Setup

### 1. Clone and install dependencies

```bash
cd d:\Workspace\opt-algo
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. Configure environment

```bash
# Copy example env file
copy config\.env.example config\.env

# Edit with your credentials:
# - DELTA_API_KEY / DELTA_API_SECRET (options account)
# - DELTA_ENVIRONMENT (testnet or production)
# - DELTA_BASE_URL (testnet or production URL)
```

**Environment URLs:**
| Environment | Base URL |
|---|---|
| Testnet | `https://cdn-ind.testnet.deltaex.org` |
| Production | `https://api.india.delta.exchange` |

### 3. Firebase (trade journaling)

Place your Firebase service account JSON at `config/firestore-service-account.json`.

Trades are stored in the `options` collection with:
- `mode: "live"` for production
- `mode: "paper"` for testnet

### 4. Run

```bash
# Test run (once, no real orders):
python main.py --once --paper

# Test run (once, with orders on testnet):
python main.py --once

# Daily schedule (production):
python main.py

# Daily schedule (no orders):
python main.py --paper
```

## Configuration

### Order Placement Kill-Switch

Set `ENABLE_ORDER_PLACEMENT=true` in `config/.env` to enable real order execution.  
Default is `false` (signal mode only).

### Strategy Parameters

Edit `config/settings.yaml` to change:
- Underlying (BTC/ETH)
- Entry/exit times
- Lot size, leverage
- Stop-loss type and value
- Monitor interval

## Discord Notifications

The bot sends the following notifications:

| Event | Content |
|---|---|
| **Startup** | Bot configuration summary, environment, mode |
| **Entry** | Spot price, ATM strike, call/put symbols, premiums, lot size, SL threshold |
| **Stop-Loss** | Current loss, SL threshold, % of premium |
| **Exit** | Entry vs exit premiums, P&L, exit reason |
| **Error** | Error details from strategy execution |

## Project Structure

```
opt-algo/
├── main.py              # Entry point (scheduler + CLI)
├── requirements.txt     # Python dependencies
├── config/
│   ├── .env.example     # Environment template
│   ├── settings.yaml    # Strategy configuration
├── core/
│   ├── config.py        # Configuration loader
│   ├── logger.py        # Structured logging
│   ├── exceptions.py    # Exception hierarchy
│   ├── error_alerts.py  # Discord error handler
│   └── firestore_client.py  # Trade journaling
├── api/
│   ├── rest_client.py   # Delta Exchange API client
│   └── rate_limiter.py  # Rate limiting
├── strategy/
│   └── short_straddle.py # Strategy engine
└── notifications/
    ├── discord.py       # Discord webhook
    └── manager.py       # Notification manager
```

## Reference

This project follows the same architecture as [delta-exchange-alog](../delta-exchange-alog) (futures trading bot).