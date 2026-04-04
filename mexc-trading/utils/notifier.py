"""Notification module - Telegram Bot integration for trade alerts."""
import logging
import threading
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)


class Notifier:
    """Send trade alerts via Telegram."""

    def __init__(self):
        self.token = getattr(Config, "TELEGRAM_BOT_TOKEN", "")
        self.chat_id = getattr(Config, "TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        if self.enabled:
            logger.info("Telegram notifications enabled")
        else:
            logger.info("Telegram notifications disabled (no token/chat_id)")

    def _send_async(self, text: str) -> None:
        """Send message in background thread to avoid blocking."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            logger.error("Telegram send failed: %s", e)

    def send(self, message: str) -> None:
        """Send a notification message."""
        if not self.enabled:
            return
        thread = threading.Thread(target=self._send_async, args=(message,), daemon=True)
        thread.start()

    def notify_start(self, config_info: dict[str, Any]) -> None:
        msg = (
            "<b>🟢 Bot Started</b>\n"
            f"Symbol: {config_info.get('symbol', '')}\n"
            f"Leverage: {config_info.get('leverage', '')}x\n"
            f"Mode: {config_info.get('mode', 'live')}\n"
            f"Order Type: {config_info.get('order_type', 'maker')}"
        )
        self.send(msg)

    def notify_entry(self, side: str, qty: float, price: float, order_type: str) -> None:
        arrow = "🔺 LONG" if side == "long" else "🔻 SHORT"
        msg = (
            f"<b>{arrow} Entry</b>\n"
            f"Price: {price:.6f}\n"
            f"Qty: {qty:.4f}\n"
            f"Type: {order_type}"
        )
        self.send(msg)

    def notify_exit(self, side: str, entry: float, exit_price: float, pnl: float) -> None:
        icon = "✅" if pnl >= 0 else "❌"
        msg = (
            f"<b>{icon} Position Closed</b>\n"
            f"Side: {side.upper()}\n"
            f"Entry: {entry:.6f} → Exit: {exit_price:.6f}\n"
            f"PnL: <b>{'+' if pnl >= 0 else ''}{pnl:.4f} USDT</b>"
        )
        self.send(msg)

    def notify_stop_loss(self, side: str, price: float, pnl: float) -> None:
        msg = (
            f"<b>🚨 STOP-LOSS Triggered</b>\n"
            f"Side: {side.upper()}\n"
            f"Price: {price:.6f}\n"
            f"PnL: <b>{pnl:.4f} USDT</b>"
        )
        self.send(msg)

    def notify_error(self, error: str) -> None:
        msg = f"<b>⚠️ Error</b>\n{error}"
        self.send(msg)

    def notify_stop(self, reason: str, balance: float) -> None:
        msg = (
            f"<b>🔴 Bot Stopped</b>\n"
            f"Reason: {reason}\n"
            f"Balance: {balance:.2f} USDT"
        )
        self.send(msg)

    def notify_daily_report(self, pnl: float, trades: int, balance: float) -> None:
        icon = "📈" if pnl >= 0 else "📉"
        msg = (
            f"<b>{icon} Daily Report</b>\n"
            f"PnL: {'+' if pnl >= 0 else ''}{pnl:.4f} USDT\n"
            f"Trades: {trades}\n"
            f"Balance: {balance:.2f} USDT"
        )
        self.send(msg)
