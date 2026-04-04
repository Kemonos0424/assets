"""MEXC Futures API client with HMAC-SHA256 authentication."""
import hashlib
import hmac
import json
import time
import logging
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)


class MEXCFuturesClient:
    """Client for MEXC Futures (Contract) API."""

    def __init__(self):
        self.api_key = Config.API_KEY
        self.secret_key = Config.SECRET_KEY
        self.base_url = Config.BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "ApiKey": self.api_key,
        })

    def _sign(self, timestamp: str, params_str: str = "") -> str:
        """Generate HMAC-SHA256 signature."""
        sign_str = self.api_key + timestamp + params_str
        return hmac.new(
            self.secret_key.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(
        self, method: str, path: str, params: dict | list | None = None, signed: bool = False
    ) -> dict[str, Any]:
        """Make an API request."""
        url = f"{self.base_url}{path}"
        timestamp = str(int(time.time() * 1000))

        headers = {}
        if signed:
            # BUG FIX: POST uses JSON body string, GET uses query string
            if method == "POST" and params is not None:
                params_str = json.dumps(params, separators=(",", ":"))
            elif method == "GET" and params:
                params_str = "&".join(f"{k}={v}" for k, v in params.items())
            else:
                params_str = ""
            headers["Request-Time"] = timestamp
            headers["Signature"] = self._sign(timestamp, params_str)

        try:
            if method == "GET":
                resp = self.session.get(url, params=params, headers=headers, timeout=10)
            elif method == "POST":
                resp = self.session.post(url, json=params, headers=headers, timeout=10)
            elif method == "DELETE":
                resp = self.session.delete(url, json=params, headers=headers, timeout=10)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0 and data.get("code") != 200:
                logger.error("API error: %s", data)
                return data

            return data
        except requests.RequestException as e:
            logger.error("Request failed: %s %s - %s", method, path, e)
            raise

    # --- Market Data ---

    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker price for a symbol."""
        return self._request("GET", "/api/v1/contract/ticker", {"symbol": symbol})

    def get_depth(self, symbol: str, limit: int = 20) -> dict:
        """Get order book depth."""
        return self._request(
            "GET", f"/api/v1/contract/depth/{symbol}",
            {"limit": limit},
        )

    def get_klines(self, symbol: str, interval: str = "Min1", limit: int = 100) -> dict:
        """Get kline/candlestick data."""
        return self._request(
            "GET",
            f"/api/v1/contract/kline/{symbol}",
            {"interval": interval, "limit": limit},
        )

    def get_contract_detail(self, symbol: str) -> dict:
        """Get contract details for a symbol."""
        return self._request("GET", "/api/v1/contract/detail", {"symbol": symbol})

    # --- Account ---

    def get_account_assets(self) -> dict:
        """Get account asset information."""
        return self._request("GET", "/api/v1/private/account/assets", signed=True)

    def get_positions(self, symbol: str | None = None) -> dict:
        """Get open positions."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request(
            "GET", "/api/v1/private/position/open_positions", params, signed=True
        )

    # --- Trading ---

    def set_leverage(self, symbol: str, leverage: int, open_type: int = 1) -> dict:
        """Set leverage for a symbol. open_type: 1=isolated, 2=cross."""
        return self._request(
            "POST",
            "/api/v1/private/position/change_leverage",
            {
                "symbol": symbol,
                "leverage": leverage,
                "openType": open_type,
                "positionType": 1,
            },
            signed=True,
        )

    def place_order(
        self,
        symbol: str,
        price: float,
        vol: float,
        side: int,
        order_type: int,
        open_type: int = 1,
        leverage: int | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> dict:
        """
        Place a futures order.

        Args:
            symbol: Trading pair (e.g., USDC_USDT)
            price: Order price (required for limit/post-only orders)
            vol: Volume in contracts
            side: 1=open long, 2=close short, 3=open short, 4=close long
            order_type: 1=limit, 2=post only (maker), 3=IOC, 4=FOK, 5=market
            open_type: 1=isolated, 2=cross
            leverage: Leverage multiplier
            stop_loss_price: Stop loss trigger price
            take_profit_price: Take profit trigger price
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "price": price,
            "vol": vol,
            "side": side,
            "type": order_type,
            "openType": open_type,
        }
        if leverage is not None:
            params["leverage"] = leverage
        if stop_loss_price is not None:
            params["stopLossPrice"] = stop_loss_price
        if take_profit_price is not None:
            params["takeProfitPrice"] = take_profit_price

        return self._request("POST", "/api/v1/private/order/submit", params, signed=True)

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """Cancel an open order."""
        return self._request(
            "POST",
            "/api/v1/private/order/cancel",
            [{"symbol": symbol, "orderId": order_id}],
            signed=True,
        )

    def cancel_all_orders(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        return self._request(
            "POST",
            "/api/v1/private/order/cancel_all",
            {"symbol": symbol},
            signed=True,
        )

    def get_open_orders(self, symbol: str) -> dict:
        """Get all open orders for a symbol."""
        return self._request(
            "GET",
            f"/api/v1/private/order/list/open_orders/{symbol}",
            signed=True,
        )

    def get_order_detail(self, symbol: str, order_id: str) -> dict:
        """Get details of a specific order."""
        return self._request(
            "GET",
            f"/api/v1/private/order/get/{order_id}",
            signed=True,
        )

    def get_order_history(self, symbol: str, page: int = 1, page_size: int = 20) -> dict:
        """Get order history."""
        return self._request(
            "GET",
            "/api/v1/private/order/list/history_orders",
            {"symbol": symbol, "page_num": page, "page_size": page_size},
            signed=True,
        )

    # --- Limit Order Helpers (Post-Only / Maker) ---

    def open_long_limit(self, symbol: str, price: float, vol: float, leverage: int) -> dict:
        """Open long with post-only limit order (maker fee = 0%)."""
        return self.place_order(
            symbol=symbol, price=price, vol=vol,
            side=1, order_type=2, leverage=leverage,
        )

    def open_short_limit(self, symbol: str, price: float, vol: float, leverage: int) -> dict:
        """Open short with post-only limit order (maker fee = 0%)."""
        return self.place_order(
            symbol=symbol, price=price, vol=vol,
            side=3, order_type=2, leverage=leverage,
        )

    def close_long_limit(self, symbol: str, price: float, vol: float) -> dict:
        """Close long with post-only limit order (maker fee = 0%)."""
        return self.place_order(
            symbol=symbol, price=price, vol=vol,
            side=4, order_type=2,
        )

    def close_short_limit(self, symbol: str, price: float, vol: float) -> dict:
        """Close short with post-only limit order (maker fee = 0%)."""
        return self.place_order(
            symbol=symbol, price=price, vol=vol,
            side=2, order_type=2,
        )

    # --- Market Order Helpers (Taker, fallback) ---

    def open_long_market(self, symbol: str, vol: float, leverage: int) -> dict:
        """Open long with market order (taker fee applies)."""
        return self.place_order(
            symbol=symbol, price=0, vol=vol,
            side=1, order_type=5, leverage=leverage,
        )

    def open_short_market(self, symbol: str, vol: float, leverage: int) -> dict:
        """Open short with market order (taker fee applies)."""
        return self.place_order(
            symbol=symbol, price=0, vol=vol,
            side=3, order_type=5, leverage=leverage,
        )

    def close_long_market(self, symbol: str, vol: float) -> dict:
        """Close long with market order (taker fee applies)."""
        return self.place_order(
            symbol=symbol, price=0, vol=vol,
            side=4, order_type=5,
        )

    def close_short_market(self, symbol: str, vol: float) -> dict:
        """Close short with market order (taker fee applies)."""
        return self.place_order(
            symbol=symbol, price=0, vol=vol,
            side=2, order_type=5,
        )
