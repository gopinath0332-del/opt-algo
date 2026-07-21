"""Discord notification handler for Options trading alerts."""

import time
from typing import Any, Dict, Optional

import requests

from core.logger import get_logger

logger = get_logger(__name__)


class DiscordNotifier:
    """Handles sending options trade notifications to Discord via Webhooks."""

    def __init__(self, webhook_url: str):
        """Initialize Discord notifier.

        Args:
            webhook_url: Discord Webhook URL
        """
        self.webhook_url = webhook_url

    def _f(self, val: Optional[float], decimals: int = 4) -> str:
        """Format currency to reasonable precision, removing trailing zeros."""
        if val is None:
            return "0"
        return f"{val:,.{decimals}f}".rstrip('0').rstrip('.')

    def _send_embed(self, title: str, description: str, color: int) -> None:
        """Send a Discord embed message.

        Args:
            title: Embed title
            description: Embed description (supports ANSI code blocks)
            color: Embed sidebar color (integer)
        """
        if not self.webhook_url:
            logger.warning("Discord webhook URL not configured")
            return

        try:
            embed = {
                "title": title,
                "description": description,
                "color": color,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            payload = {"embeds": [embed]}
            response = requests.post(self.webhook_url, json=payload, timeout=5)
            response.raise_for_status()
            logger.debug("Discord notification sent")
        except requests.RequestException as e:
            logger.error("Discord connection failed", error=str(e))
        except Exception as e:
            logger.error("Failed to send Discord notification", error=str(e))

    def send_entry_alert(
        self,
        underlying: str,
        strategy_name: str,
        spot_price: float,
        atm_strike: float,
        call_symbol: str,
        put_symbol: str,
        call_premium: float,
        put_premium: float,
        total_premium: float,
        lot_size: int,
        account_balance: Optional[float],
        sl_threshold: float,
        mode: str = "live",
        entry_slippage_usd: Optional[float] = None,
        margin_usd: Optional[float] = None,
    ) -> None:
        """Send a straddle entry notification."""
        mode_color = "1;32" if mode == "live" else "1;36"
        sl_str = "Disabled" if sl_threshold == float('inf') else f"${self._f(sl_threshold)}"

        message = (
            f"Strategy: \u001b[1;37m{strategy_name}\u001b[0m\n"
            f"Mode: \u001b[{mode_color}m{mode.upper()}\u001b[0m\n"
            f"Underlying: \u001b[1;37m{underlying}\u001b[0m\n"
            f"\n"
            f"Spot Price: \u001b[0;36m${self._f(spot_price, 2)}\u001b[0m\n"
            f"ATM Strike: \u001b[0;36m${self._f(atm_strike, 2)}\u001b[0m\n"
            f"\n"
            f"\u001b[1;37mCALL LEG:\u001b[0m {call_symbol}\n"
            f"  Premium: \u001b[0;33m{self._f(call_premium)} points\u001b[0m\n"
            f"\u001b[1;37mPUT LEG:\u001b[0m {put_symbol}\n"
            f"  Premium: \u001b[0;33m{self._f(put_premium)} points\u001b[0m\n"
            f"\n"
            f"Total Premium: \u001b[0;32m{self._f(total_premium)} points\u001b[0m\n"
            f"Lot Size: \u001b[0;36m{lot_size}\u001b[0m per leg\n"
        )

        if margin_usd is not None:
            message += f"Margin Locked: \u001b[0;33m${self._f(margin_usd, 2)}\u001b[0m\n"

        message += (
            f"Account Balance: \u001b[0;35m${self._f(account_balance, 2) if account_balance is not None else 'N/A'}\u001b[0m\n"
            f"Combined SL: \u001b[0;31m{sl_str}\u001b[0m\n"
        )

        if entry_slippage_usd is not None:
            message += f"Entry Slippage: \u001b[0;35m${self._f(entry_slippage_usd, 2)}\u001b[0m\n"

        message += f"\nTime: {time.strftime('%H:%M:%S IST')}"

        formatted = f"```ansi\n{message}\n```"
        title = f"📊 SHORT STRADDLE ENTRY — {underlying} @ Strike {self._f(atm_strike, 0)}"
        color = 5763719  # Green

        self._send_embed(title, formatted, color)

    def send_exit_alert(
        self,
        underlying: str,
        exit_reason: str,
        entry_premium: float,
        exit_premium: float,
        realized_pnl: float,
        call_symbol: str,
        put_symbol: str,
        exit_call_premium: float,
        exit_put_premium: float,
        mode: str = "live",
        exit_slippage_usd: Optional[float] = None,
        total_slippage_usd: Optional[float] = None,
    ) -> None:
        """Send a straddle exit notification."""
        pnl_color = "0;32" if realized_pnl >= 0 else "0;31"
        pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"
        mode_color = "1;32" if mode == "live" else "1;36"

        message = (
            f"Mode: \u001b[{mode_color}m{mode.upper()}\u001b[0m\n"
            f"Reason: \u001b[1;37m{exit_reason}\u001b[0m\n"
            f"\n"
            f"\u001b[1;37mCALL:\u001b[0m {call_symbol}\n"
            f"  Exit Premium: \u001b[0;33m${self._f(exit_call_premium)}\u001b[0m\n"
            f"\u001b[1;37mPUT:\u001b[0m {put_symbol}\n"
            f"  Exit Premium: \u001b[0;33m${self._f(exit_put_premium)}\u001b[0m\n"
            f"\n"
            f"Entry Premium: \u001b[0;36m${self._f(entry_premium)}\u001b[0m\n"
            f"Exit Premium: \u001b[0;36m${self._f(exit_premium)}\u001b[0m\n"
            f"Realized P&L: \u001b[{pnl_color}m${self._f(realized_pnl)}\u001b[0m\n"
        )

        if exit_slippage_usd is not None:
            message += f"Exit Slippage: \u001b[0;35m${self._f(exit_slippage_usd, 2)}\u001b[0m\n"
        if total_slippage_usd is not None:
            message += f"Total Slippage: \u001b[0;35m${self._f(total_slippage_usd, 2)}\u001b[0m\n"

        message += f"\nTime: {time.strftime('%H:%M:%S IST')}"

        formatted = f"```ansi\n{message}\n```"
        title = f"{pnl_emoji} SHORT STRADDLE EXIT — {underlying} | {exit_reason}"
        color = 5763719 if realized_pnl >= 0 else 15548997  # Green or Red

        self._send_embed(title, formatted, color)

    def send_sl_alert(
        self,
        underlying: str,
        current_loss: float,
        sl_threshold: float,
        entry_premium: float,
        loss_pct: float,
        mode: str = "live",
    ) -> None:
        """Send a stop-loss hit notification.

        Args:
            underlying: Underlying asset
            current_loss: Current MTM loss
            sl_threshold: SL threshold that was hit
            entry_premium: Premium collected at entry
            loss_pct: Loss as % of premium
            mode: 'live' or 'paper'
        """
        mode_color = "1;32" if mode == "live" else "1;36"

        message = (
            f"Mode: \u001b[{mode_color}m{mode.upper()}\u001b[0m\n"
            f"\n"
            f"\u001b[0;31m⚠️ COMBINED STOP-LOSS TRIGGERED\u001b[0m\n"
            f"\n"
            f"Current Loss: \u001b[0;31m${self._f(abs(current_loss))}\u001b[0m\n"
            f"SL Threshold: \u001b[0;33m${self._f(sl_threshold)}\u001b[0m\n"
            f"Entry Premium: \u001b[0;36m${self._f(entry_premium)}\u001b[0m\n"
            f"Loss: \u001b[0;31m{loss_pct:.1f}%\u001b[0m of premium\n"
            f"\n"
            f"Action: \u001b[1;37mClosing all positions...\u001b[0m\n"
            f"Time: {time.strftime('%H:%M:%S IST')}"
        )

        formatted = f"```ansi\n{message}\n```"
        title = f"🛑 STOP-LOSS HIT — {underlying} Short Straddle"
        color = 15548997  # Red

        self._send_embed(title, formatted, color)

    def send_status_message(self, title: str, message: str, color: int = 3447003) -> None:
        """Send a general status message.

        Args:
            title: Message title
            message: Message content (ANSI formatted)
            color: Embed color
        """
        formatted = f"```ansi\n{message}\n```"
        self._send_embed(title, formatted, color)

    def send_error(self, title: str, error: str) -> None:
        """Send an error alert.

        Args:
            title: Error title
            error: Error details
        """
        message = f"\u001b[0;31mError:\u001b[0m {error}"
        formatted = f"```ansi\n{message}\n```"
        self._send_embed(f"⚠️ {title}", formatted, 15158332)

    def send_trade_alert(
        self,
        symbol: str,
        side: str,
        price: float,
        reason: str,
        rsi: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        lot_size: Optional[int] = None,
        pnl: Optional[float] = None,
        hold_duration: Optional[str] = None,
        strategy_name: str = "Strategy",
        timeframe: str = "1h",
        mode: str = "live",
    ) -> None:
        """Send a general trade alert (entry or exit)."""
        mode_color = "1;32" if mode == "live" else "1;36"
        is_entry = "ENTRY" in side.upper()

        if is_entry:
            title = f"🚀 TRADING SIGNAL: {side} {symbol} ({timeframe})"
            color = 3066993 if "LONG" in side.upper() else 15158332
            message = (
                f"Strategy: \u001b[1;37m{strategy_name}\u001b[0m\n"
                f"Mode: \u001b[{mode_color}m{mode.upper()}\u001b[0m\n"
                f"Price: \u001b[0;36m${self._f(price, 2)}\u001b[0m\n"
            )
            if stop_loss_price:
                message += f"Stop Loss: \u001b[0;31m${self._f(stop_loss_price, 2)}\u001b[0m\n"
            if take_profit_price:
                message += f"Take Profit: \u001b[0;32m${self._f(take_profit_price, 2)}\u001b[0m\n"
            if lot_size:
                message += f"Lot Size: \u001b[0;36m{lot_size}\u001b[0m contracts\n"
            message += f"Reason: \u001b[1;37m{reason}\u001b[0m\n"
        else:
            pnl_color = "0;32" if (pnl or 0) >= 0 else "0;31"
            title = f"🚀 TRADING SIGNAL: {side} {symbol} ({timeframe})"
            color = 3066993 if (pnl or 0) >= 0 else 15158332
            message = (
                f"Strategy: \u001b[1;37m{strategy_name}\u001b[0m\n"
                f"Mode: \u001b[{mode_color}m{mode.upper()}\u001b[0m\n"
                f"Price: \u001b[0;36m${self._f(price, 2)}\u001b[0m\n"
            )
            if pnl is not None:
                message += f"P&L: \u001b[{pnl_color}m${self._f(pnl, 2)}\u001b[0m\n"
            if hold_duration:
                message += f"Hold Duration: \u001b[0;36m{hold_duration}\u001b[0m\n"
            message += f"Reason: \u001b[1;37m{reason}\u001b[0m\n"

        formatted = f"```ansi\n{message}\n```"
        self._send_embed(title, formatted, color)


