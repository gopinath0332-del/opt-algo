"""
BTC Delta Scanner — find CALL closest to delta 0.36 and send Discord alert.

Usage:
    # Run immediately (fire now):
    python delta_scanner.py --now

    # Schedule for 16:30 IST today (default behaviour):
    python delta_scanner.py

    # Override target delta:
    python delta_scanner.py --delta 0.25 --now
"""

import argparse
import datetime
import sys
import time
import zoneinfo
from typing import Any, Dict, List, Optional, Tuple

import requests
import io

# ---------------------------------------------------------------------------
# Bootstrap: load env + project imports
# ---------------------------------------------------------------------------
import os

# Fix Windows console encoding (cp1252 -> utf-8) so emoji / Greek letters print
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
elif sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from pathlib import Path

# Ensure the project root is on sys.path
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(str(_ROOT / "config" / ".env"))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TARGET_DELTA   = 0.36          # desired call delta
UNDERLYING     = "BTC"
IST            = zoneinfo.ZoneInfo("Asia/Kolkata")
FIRE_TIME_IST  = (16, 30)      # (hour, minute) 16:30 IST

BASE_URL       = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
WEBHOOK_URL    = os.getenv("DISCORD_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(val: Optional[float], decimals: int = 4) -> str:
    """Format float nicely, removing trailing zeros only from decimal portion."""
    if val is None:
        return "—"
    if decimals == 0:
        return f"{int(round(val)):,}"
    s = f"{val:,.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def send_discord_startup_message(
    target_delta: float,
    expiry_filter: str,
    fire_time: Tuple[int, int],
    spot_price: Optional[float] = None,
) -> None:
    """Send a startup alert to Discord when the scanner service initializes."""
    h, m = fire_time
    now_ist = datetime.datetime.now(IST).strftime("%H:%M:%S IST · %d %b %Y")
    spot_str = f"${_fmt(spot_price, 2)}" if spot_price else "—"

    ansi = (
        f"\u001b[1;34m📡 BTC Options — Delta Scanner Service\u001b[0m\n"
        f"Status       : \u001b[1;32mONLINE\u001b[0m\n"
        f"Schedule     : \u001b[1;36mDaily at {h:02d}:{m:02d} IST\u001b[0m\n"
        f"Target Delta : \u001b[1;36m±{target_delta:.2f}\u001b[0m  (independent legs)\n"
        f"Expiry Mode  : \u001b[0;37m{expiry_filter}\u001b[0m\n"
        f"BTC Spot     : \u001b[0;36m{spot_str}\u001b[0m\n\n"
        f"\u001b[0;37mService initialized and waiting for scheduled execution.\u001b[0m\n"
        f"\u001b[0;37mStarted at   : {now_ist}\u001b[0m"
    )

    payload = {
        "embeds": [{
            "title": f"🚀 BTC Delta Scanner Online | Schedule {h:02d}:{m:02d} IST",
            "description": f"```ansi\n{ansi}\n```",
            "color": 3447003,  # Blue
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "footer": {"text": "Delta Scanner · Service Online · opt-algo"},
        }]
    }

    if not WEBHOOK_URL:
        print("  ⚠️  DISCORD_WEBHOOK_URL not set — skipping Discord startup send.")
        return

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  ✅ Discord startup notification sent (HTTP {r.status_code})")
    except requests.RequestException as e:
        print(f"  ⚠️ Discord startup notification failed: {e}")


def _get(endpoint: str, params: Optional[Dict] = None) -> Any:
    """Simple public GET with exponential backoff retry."""
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}] {e} — waiting {wait}s")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------

def fetch_btc_option_products() -> List[Dict[str, Any]]:
    """Return all live BTC call_options and put_options."""
    data = _get("/v2/products")
    products = data.get("result", [])
    btc_opts = [
        p for p in products
        if p.get("contract_type") in ("call_options", "put_options")
        and p.get("state") == "live"
        and str(p.get("underlying_asset", {}).get("symbol", "")).upper() == UNDERLYING.upper()
    ]
    print(f"  Found {len(btc_opts)} live BTC option products")
    return btc_opts


def fetch_all_tickers() -> Dict[str, Dict[str, Any]]:
    """Fetch ALL tickers in one call — returns a dict keyed by symbol.

    Delta Exchange supports calling /v2/tickers without a symbol param to
    return every live instrument's ticker, including greeks (delta, gamma…).
    This avoids making one HTTP call per option contract.
    """
    data = _get("/v2/tickers")
    result = data.get("result", {})
    ticker_map: Dict[str, Dict[str, Any]] = {}

    if isinstance(result, list):
        for item in result:
            sym = item.get("symbol")
            if sym:
                ticker_map[sym] = item
    elif isinstance(result, dict):
        # May be {symbol: ticker_dict, ...} or a single ticker dict
        for key, val in result.items():
            if isinstance(val, dict):
                ticker_map[key] = val
            else:
                # Flat dict — single ticker (unlikely for no-symbol call)
                ticker_map[result.get("symbol", "")] = result
                break

    print(f"  Fetched {len(ticker_map)} tickers in one bulk call")
    return ticker_map


def fetch_ticker(symbol: str) -> Dict[str, Any]:
    """Fetch ticker for a single symbol (fallback for individual lookups)."""
    data = _get("/v2/tickers", params={"symbol": symbol})
    result = data.get("result", {})
    if isinstance(result, list):
        for item in result:
            if item.get("symbol") == symbol:
                return item
        return {}
    if isinstance(result, dict):
        if symbol in result:
            return result[symbol]
        return result
    return {}


def get_delta_from_ticker(ticker: Dict[str, Any]) -> Optional[float]:
    """Extract delta value from ticker (Delta Exchange exposes it as top-level 'delta')."""
    if "delta" in ticker:
        val = ticker["delta"]
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    greeks = ticker.get("greeks") or {}
    if isinstance(greeks, dict) and "delta" in greeks:
        val = greeks["delta"]
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


def get_mark_price_from_ticker(ticker: Dict[str, Any]) -> Optional[float]:
    """Extract mark price (= premium) from ticker."""
    for key in ("mark_price", "close", "last_price"):
        val = ticker.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


def find_target_options(
    products: List[Dict[str, Any]],
    all_tickers: Dict[str, Dict[str, Any]],
    target_delta: float = 0.36,
    expiry_filter: str = "same",
) -> Tuple[Optional[Dict], Optional[Dict], Optional[Dict], Optional[Dict]]:
    """
    Independently find:
      - The BTC CALL whose delta is closest to +target_delta
      - The BTC PUT  whose delta is closest to -target_delta  (puts have negative delta)

    expiry_filter:
      - 'same'  : (default) guarantees Call and Put share the same expiry date,
                  choosing the expiry whose combined delta difference is smallest.
      - 'daily' or 'next': filters to the next upcoming active expiry (>1h away).
      - 'any'   : completely unconstrained (legs can have different expiries).
      - <str>   : substring filter on settlement_time or symbol (e.g. '041026').

    Returns:
        (call_product, call_ticker, put_product, put_ticker)
    """
    def _get_exp_str(p: Dict[str, Any]) -> str:
        return str(p.get("settlement_time") or p.get("expiry_date") or "")

    def _get_exp_ts(p: Dict[str, Any]) -> float:
        raw = _get_exp_str(p)
        try:
            return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            return float("inf")

    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    # Pre-filter by expiry mode if requested
    exp_lower = expiry_filter.lower().strip()
    if exp_lower in ("daily", "next"):
        # Select the next upcoming expiry that has at least 1 hour remaining
        all_exp_ts = sorted(set(_get_exp_ts(p) for p in products if _get_exp_ts(p) > now_ts + 3600))
        if all_exp_ts:
            target_ts = all_exp_ts[0]
            products = [p for p in products if abs(_get_exp_ts(p) - target_ts) < 60]
            print(f"  [Filter] Selected next expiry: {_expiry_label(products[0])}")
        else:
            print("  [WARN] No upcoming expiry > 1h away found; using all products.")
    elif exp_lower not in ("same", "any", "all"):
        # Specific date substring (e.g. '041026', '2026-10-04')
        filtered = [p for p in products if exp_lower in _get_exp_str(p).lower() or exp_lower in p.get("symbol", "").lower()]
        if filtered:
            products = filtered
            print(f"  [Filter] Matched {len(products)} products matching '{expiry_filter}'")
        else:
            print(f"  [WARN] No products matched expiry filter '{expiry_filter}'; scanning all.")

    def _best_in_subset(
        subset: List[Dict[str, Any]],
        target: float,
        ctype: str,
    ) -> Tuple[Optional[Dict], Optional[Dict], float]:
        best_p, best_t, best_d = None, None, float("inf")
        for p in subset:
            if p.get("contract_type") != ctype:
                continue
            sym = p.get("symbol")
            if not sym:
                continue
            tick = all_tickers.get(sym)
            if tick is None:
                try:
                    tick = fetch_ticker(sym)
                except Exception:
                    continue
            delta = get_delta_from_ticker(tick)
            if delta is None:
                raw_d = p.get("delta")
                if raw_d is not None:
                    try:
                        delta = float(raw_d)
                    except (ValueError, TypeError):
                        pass
            if delta is None:
                continue

            diff = abs(delta - target)
            if diff < best_d:
                best_d = diff
                best_p = p
                best_t = tick
        return best_p, best_t, best_d

    if exp_lower == "same":
        # Group by expiry date and find the expiry where BOTH legs are closest to 0.36
        exp_groups: Dict[str, List[Dict[str, Any]]] = {}
        for p in products:
            exp_groups.setdefault(_get_exp_str(p), []).append(p)

        best_pair = None
        min_combined_diff = float("inf")

        print(f"\n  Evaluating {len(exp_groups)} expiries for best simultaneous CALL (+{target_delta:.2f}) & PUT (-{target_delta:.2f})...")
        for exp_str, prods in sorted(exp_groups.items()):
            c_prod, c_tick, c_diff = _best_in_subset(prods, +target_delta, "call_options")
            p_prod, p_tick, p_diff = _best_in_subset(prods, -target_delta, "put_options")
            if c_prod and p_prod:
                combined_diff = c_diff + p_diff
                c_delta = get_delta_from_ticker(c_tick)
                p_delta = get_delta_from_ticker(p_tick)
                print(
                    f"    Expiry {_expiry_label(c_prod):22s}: "
                    f"CALL {c_prod.get('symbol'):28s} (Δ={c_delta:+.4f}) | "
                    f"PUT {p_prod.get('symbol'):28s} (Δ={p_delta:+.4f}) | "
                    f"combined diff={combined_diff:.4f}"
                )
                if combined_diff < min_combined_diff:
                    min_combined_diff = combined_diff
                    best_pair = (c_prod, c_tick, p_prod, p_tick)

        if best_pair:
            c_prod, c_tick, p_prod, p_tick = best_pair
            print(f"\n  [OK] Selected same-expiry pair on {_expiry_label(c_prod)} (combined diff={min_combined_diff:.4f})")
            return c_prod, c_tick, p_prod, p_tick
        print("  [WARN] Could not find common expiry with both call and put; falling back to independent scan.")

    # Independent / fallback scan
    calls = [p for p in products if p.get("contract_type") == "call_options"]
    puts  = [p for p in products if p.get("contract_type") == "put_options"]

    print(f"\n  Scanning {len(calls)} CALL options for delta ~+{target_delta:.2f} ...")
    call_prod, call_tick, _ = _best_in_subset(calls, +target_delta, "call_options")
    if call_prod:
        c_del = get_delta_from_ticker(call_tick)
        print(f"  [OK] Best CALL: {call_prod.get('symbol')} (delta={c_del:+.4f})")

    print(f"\n  Scanning {len(puts)} PUT options for delta ~-{target_delta:.2f} ...")
    put_prod, put_tick, _ = _best_in_subset(puts, -target_delta, "put_options")
    if put_prod:
        p_del = get_delta_from_ticker(put_tick)
        print(f"  [OK] Best PUT : {put_prod.get('symbol')} (delta={p_del:+.4f}, |Δ|={abs(p_del):.4f})")

    return call_prod, call_tick, put_prod, put_tick


# ---------------------------------------------------------------------------
# Discord message builder
# ---------------------------------------------------------------------------

def _expiry_label(product: Dict[str, Any]) -> str:
    """Human-readable expiry string in IST."""
    raw = product.get("settlement_time") or product.get("expiry_date") or ""
    try:
        dt = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.astimezone(IST).strftime("%d %b %Y %H:%M IST")
    except Exception:
        return str(raw) if raw else "—"


def send_discord_alert(
    call_product: Dict[str, Any],
    call_ticker: Dict[str, Any],
    put_product: Optional[Dict[str, Any]],
    put_ticker: Optional[Dict[str, Any]],
    target_delta: float,
    spot_price: Optional[float] = None,
) -> None:
    """Build and post a rich Discord embed with CALL + PUT details."""

    # ── Extract values ───────────────────────────────────────────────────────
    call_symbol  = call_product.get("symbol", "—")
    call_strike  = float(call_product.get("strike_price", 0))
    call_expiry  = _expiry_label(call_product)
    call_delta   = get_delta_from_ticker(call_ticker) or 0.0
    call_premium = get_mark_price_from_ticker(call_ticker)

    put_symbol   = put_product.get("symbol", "—")   if put_product else "—"
    put_strike   = float(put_product.get("strike_price", 0)) if put_product else call_strike
    put_expiry   = _expiry_label(put_product)         if put_product else call_expiry
    put_delta    = get_delta_from_ticker(put_ticker)  if put_ticker  else None
    put_premium  = get_mark_price_from_ticker(put_ticker) if put_ticker else None

    now_ist = datetime.datetime.now(IST).strftime("%H:%M IST · %d %b %Y")

    ansi  = f"\u001b[1;33m📡 BTC Options — Delta Scout\u001b[0m\n"
    ansi += f"\u001b[0;37mTarget Delta : \u001b[1;36m±{target_delta:.2f}  (independent legs)\u001b[0m\n"
    if spot_price:
        ansi += f"\u001b[0;37mBTC Spot     : \u001b[0;36m${_fmt(spot_price, 2)}\u001b[0m\n"

    ansi += (
        f"\n"
        f"\u001b[1;32m── CALL ─────────────────────────────────────\u001b[0m\n"
        f"Symbol   : \u001b[1;37m{call_symbol}\u001b[0m\n"
        f"Strike   : \u001b[0;36m${_fmt(call_strike, 0)}\u001b[0m\n"
        f"Expiry   : \u001b[0;37m{call_expiry}\u001b[0m\n"
        f"Delta    : \u001b[1;32m{call_delta:+.4f}\u001b[0m\n"
        f"Premium  : \u001b[0;33m{_fmt(call_premium, 4)} pts\u001b[0m\n"
        f"\n"
        f"\u001b[1;31m── PUT ──────────────────────────────────────\u001b[0m\n"
        f"Symbol   : \u001b[1;37m{put_symbol}\u001b[0m\n"
        f"Strike   : \u001b[0;36m${_fmt(put_strike, 0)}\u001b[0m\n"
        f"Expiry   : \u001b[0;37m{put_expiry}\u001b[0m\n"
    )
    if put_delta is not None:
        ansi += (
            f"Delta    : \u001b[1;31m{put_delta:+.4f}\u001b[0m"
            f"  \u001b[0;37m(|Δ|={abs(put_delta):.4f})\u001b[0m\n"
        )
    else:
        ansi += "Delta    : \u001b[0;37m— (unavailable)\u001b[0m\n"
    ansi += f"Premium  : \u001b[0;33m{_fmt(put_premium, 4)} pts\u001b[0m\n"

    ansi += f"\n\u001b[0;37m⏱ Scanned: {now_ist}\u001b[0m"

    description = f"```ansi\n{ansi}\n```"
    put_strike_str = _fmt(put_strike, 0) if put_product else "—"
    title = (
        f"📊 BTC Strangle | CALL ${_fmt(call_strike, 0)} / PUT ${put_strike_str}"
        f" | Delta ~{target_delta:.2f}"
    )

    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": 3066993,  # green
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "footer": {"text": "Delta Scanner · No order placed · opt-algo"},
        }]
    }

    if not WEBHOOK_URL:
        print("  ⚠️  DISCORD_WEBHOOK_URL not set — skipping Discord send.")
        print(f"\n  Title: {title}")
        print(f"  Body preview:\n{ansi}")
        return

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  ✅ Discord alert sent  (HTTP {r.status_code})")
    except requests.RequestException as e:
        print(f"  ❌ Discord send failed: {e}")


# ---------------------------------------------------------------------------
# BTC spot helper
# ---------------------------------------------------------------------------

def get_btc_spot() -> Optional[float]:
    """Fetch current BTC mark price from perpetual ticker."""
    try:
        data = _get("/v2/tickers", params={"symbol": "BTCUSD"})
        result = data.get("result", {})
        if isinstance(result, dict):
            val = result.get("mark_price") or result.get("close") or result.get("last_price")
            if val:
                return float(val)
        if isinstance(result, list):
            for item in result:
                if item.get("symbol") == "BTCUSD":
                    val = item.get("mark_price") or item.get("close")
                    if val:
                        return float(val)
    except Exception as e:
        print(f"  ⚠️  Could not fetch BTC spot: {e}")
    return None


# ---------------------------------------------------------------------------
# Scheduler helper
# ---------------------------------------------------------------------------

def seconds_until(hour: int, minute: int) -> float:
    """Return seconds from now until the next HH:MM IST (today or tomorrow)."""
    now    = datetime.datetime.now(IST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_scan(target_delta: float, expiry_filter: str = "same") -> None:
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  BTC DELTA SCANNER  |  target delta ~ {target_delta:.2f}  |  expiry: {expiry_filter}")
    print(f"  {datetime.datetime.now(IST).strftime('%H:%M:%S IST, %d %b %Y')}")
    print(f"{sep}\n")

    # ── Step 1: fetch all products ──────────────────────────────────────────
    products = fetch_btc_option_products()
    if not products:
        print("  [ERR] No BTC option products found.")
        return

    # ── Step 2: one bulk ticker call (much faster than per-contract) ────────
    print("  Fetching all tickers (single bulk call)...")
    all_tickers = fetch_all_tickers()

    # Extract BTC spot from the bulk map (no extra HTTP call needed)
    spot: Optional[float] = None
    btc_tick = all_tickers.get("BTCUSD") or all_tickers.get("BTC_USDT") or {}
    for key in ("mark_price", "close", "last_price"):
        val = btc_tick.get(key)
        if val:
            try:
                spot = float(val)
                break
            except (ValueError, TypeError):
                pass
    if spot is None:
        spot = get_btc_spot()   # fallback to dedicated call
    if spot:
        print(f"  BTC Spot: ${spot:,.2f}\n")

    # ── Step 3: find best CALL + matching PUT ───────────────────────────────
    call_prod, call_tick, put_prod, put_tick = find_target_options(
        products, all_tickers, target_delta, expiry_filter
    )

    if call_prod is None:
        print("  [ERR] Could not locate a CALL option with delta data.")
        return

    # ── Step 4: send Discord ────────────────────────────────────────────────
    print()
    send_discord_alert(call_prod, call_tick, put_prod, put_tick, target_delta, spot)

    print(f"\n{sep}\n  Done.\n{sep}\n")


def check_single_instance() -> None:
    """Ensure only one scanner instance runs concurrently on this machine."""
    import tempfile
    lock_file = Path(tempfile.gettempdir()) / "delta_scanner.pid"
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            if pid != os.getpid():
                if sys.platform == "win32":
                    import ctypes
                    SYNCHRONIZE = 0x00100000
                    h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                    if h != 0:
                        ctypes.windll.kernel32.CloseHandle(h)
                        print(f"\n  ⚠️  Another delta_scanner instance is already running on this machine (PID {pid}).")
                        print("     Exiting to prevent duplicate Discord messages.\n")
                        sys.exit(0)
                else:
                    os.kill(pid, 0)
                    print(f"\n  ⚠️  Another delta_scanner instance is already running on this machine (PID {pid}).")
                    print("     Exiting to prevent duplicate Discord messages.\n")
                    sys.exit(0)
        except (ValueError, OSError):
            pass
    try:
        lock_file.write_text(str(os.getpid()))
        import atexit
        atexit.register(lambda: lock_file.unlink(missing_ok=True))
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC Options Delta Scanner — sends Discord alert (no orders placed)"
    )
    parser.add_argument(
        "--now", action="store_true",
        help="Fire immediately instead of waiting for 16:30 IST",
    )
    parser.add_argument(
        "--delta", type=float, default=TARGET_DELTA,
        metavar="DELTA",
        help=f"Target delta, default={TARGET_DELTA}",
    )
    parser.add_argument(
        "--expiry", type=str, default="same",
        metavar="EXPIRY",
        help="Expiry filter: 'same' (default, same expiry best match), 'daily'/'next' (next daily contract), 'any' (unconstrained), or date substring like '041026'",
    )
    args = parser.parse_args()

    if args.now:
        run_scan(args.delta, args.expiry)
        return

    # Ensure single instance on this machine
    check_single_instance()

    # ── Scheduled daily mode ──────────────────────────────────────────────────
    h, m = FIRE_TIME_IST
    print(f"\n⏰  Scheduled BTC Delta Scanner active (Daily at {h:02d}:{m:02d} IST)")
    print(f"    Target Δ : {args.delta:.2f}")
    print(f"    Expiry   : {args.expiry}")
    print("    Press Ctrl+C to cancel.\n")

    # Send startup message to Discord
    spot = get_btc_spot()
    send_discord_startup_message(args.delta, args.expiry, FIRE_TIME_IST, spot)

    while True:
        wait = seconds_until(h, m)
        fire_at = (
            datetime.datetime.now(IST) + datetime.timedelta(seconds=wait)
        ).strftime("%H:%M IST, %d %b %Y")

        print(f"  [Waiting] Next scan triggers at: {fire_at} (in {wait/60:.1f} min)")

        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            print("\n  Cancelled by user.")
            return

        try:
            run_scan(args.delta, args.expiry)
        except Exception as e:
            print(f"  ❌ Error during scheduled scan: {e}")

        # Sleep 65s so seconds_until recalculates for the next day (tomorrow)
        time.sleep(65)


if __name__ == "__main__":
    main()
