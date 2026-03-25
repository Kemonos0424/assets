"""
USDC/USDT Futures Arbitrage Strategy.

USDC/USDT is a stablecoin pair that typically trades very close to 1.0000.
This strategy exploits small deviations from the peg using leveraged futures:

- When USDC/USDT drops below the threshold (e.g., 0.998), open LONG
  expecting mean reversion back to 1.0
- When USDC/USDT rises above the threshold (e.g., 1.002), open SHORT
  expecting mean reversion back to 1.0
- With leverage, even small 0.1-0.2% moves generate meaningful returns
"""
import logging
import time
from dataclasses import dataclass

from mexc_client import MEXCFuturesClient
from config import Config
from utils.risk_manager import RiskManager

logger = logging.getLogger(__name__)


@dataclass
class Position:
    side: str  # "long" or "short"
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    open_time: float


class USDCUSDTStrategy:
    """Mean-reversion strategy for USDC/USDT futures pair."""

    FAIR_VALUE = 1.0000  # USDC/USDT fair value (peg)

    def __init__(self, client: MEXCFuturesClient, risk_manager: RiskManager):
        self.client = client
        self.risk = risk_manager
        self.symbol = Config.SYMBOL
        self.leverage = Config.LEVERAGE
        self.threshold = Config.PRICE_DEVIATION_THRESHOLD
        self.trade_amount = Config.TRADE_AMOUNT_USDT
        self.current_position: Position | None = None
        self._setup_leverage()

    def _setup_leverage(self) -> None:
        """Configure leverage on the exchange."""
        try:
            result = self.client.set_leverage(self.symbol, self.leverage)
            logger.info("Leverage set to %dx: %s", self.leverage, result)
        except Exception as e:
            logger.error("Failed to set leverage: %s", e)

    def get_current_price(self) -> float | None:
        """Fetch the current mid price for USDC/USDT."""
        try:
            ticker = self.client.get_ticker(self.symbol)
            data = ticker.get("data", {})
            if isinstance(data, list) and data:
                return float(data[0].get("lastPrice", 0))
            if isinstance(data, dict):
                return float(data.get("lastPrice", 0))
            return None
        except Exception as e:
            logger.error("Failed to get price: %s", e)
            return None

    def get_account_balance(self) -> float:
        """Get available USDT balance."""
        try:
            assets = self.client.get_account_assets()
            data = assets.get("data", [])
            for asset in data:
                if asset.get("currency") == "USDT":
                    return float(asset.get("availableBalance", 0))
            return 0.0
        except Exception as e:
            logger.error("Failed to get balance: %s", e)
            return 0.0

    def check_existing_positions(self) -> None:
        """Sync current position state from the exchange."""
        try:
            positions = self.client.get_positions(self.symbol)
            data = positions.get("data", [])
            if not data:
                self.current_position = None
                return

            for pos in data:
                vol = float(pos.get("holdVol", 0))
                if vol > 0:
                    side = "long" if pos.get("positionType") == 1 else "short"
                    self.current_position = Position(
                        side=side,
                        entry_price=float(pos.get("openAvg", 0)),
                        quantity=vol,
                        stop_loss=self.risk.calculate_stop_loss(
                            float(pos.get("openAvg", 0)), side
                        ),
                        take_profit=self.risk.calculate_take_profit(
                            float(pos.get("openAvg", 0)), side
                        ),
                        open_time=time.time(),
                    )
                    logger.info(
                        "Existing position found: %s %.4f @ %.6f",
                        side,
                        vol,
                        self.current_position.entry_price,
                    )
                    return

            self.current_position = None
        except Exception as e:
            logger.error("Failed to check positions: %s", e)

    def evaluate_signal(self, price: float) -> str | None:
        """
        Determine trading signal based on price deviation from peg.

        Returns: "long", "short", or None
        """
        deviation = price - self.FAIR_VALUE

        if deviation < -self.threshold:
            logger.info(
                "LONG signal: price=%.6f, deviation=%.6f (below -%.4f threshold)",
                price,
                deviation,
                self.threshold,
            )
            return "long"

        if deviation > self.threshold:
            logger.info(
                "SHORT signal: price=%.6f, deviation=%.6f (above +%.4f threshold)",
                price,
                deviation,
                self.threshold,
            )
            return "short"

        return None

    def should_close_position(self, price: float) -> bool:
        """Check if current position should be closed."""
        if self.current_position is None:
            return False

        pos = self.current_position

        if pos.side == "long":
            if price <= pos.stop_loss:
                logger.warning("Stop-loss triggered for LONG at %.6f", price)
                return True
            if price >= pos.take_profit:
                logger.info("Take-profit triggered for LONG at %.6f", price)
                return True
            # Close when price reverts to fair value
            if price >= self.FAIR_VALUE:
                logger.info("Fair value reached for LONG at %.6f", price)
                return True

        elif pos.side == "short":
            if price >= pos.stop_loss:
                logger.warning("Stop-loss triggered for SHORT at %.6f", price)
                return True
            if price <= pos.take_profit:
                logger.info("Take-profit triggered for SHORT at %.6f", price)
                return True
            # Close when price reverts to fair value
            if price <= self.FAIR_VALUE:
                logger.info("Fair value reached for SHORT at %.6f", price)
                return True

        return False

    def open_position(self, signal: str, price: float) -> bool:
        """Open a new position based on the signal."""
        balance = self.get_account_balance()
        quantity = self.risk.calculate_position_size(balance, price, self.leverage)

        if quantity <= 0:
            logger.warning("Insufficient balance for new position")
            return False

        current_value = quantity * price
        if not self.risk.can_open_trade(current_value):
            return False

        stop_loss = self.risk.calculate_stop_loss(price, signal)
        take_profit = self.risk.calculate_take_profit(price, signal)

        try:
            if signal == "long":
                result = self.client.open_long(self.symbol, quantity, self.leverage)
            else:
                result = self.client.open_short(self.symbol, quantity, self.leverage)

            if result.get("code") == 0:
                self.current_position = Position(
                    side=signal,
                    entry_price=price,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    open_time=time.time(),
                )
                logger.info(
                    "Opened %s position: qty=%.4f, price=%.6f, SL=%.6f, TP=%.6f",
                    signal,
                    quantity,
                    price,
                    stop_loss,
                    take_profit,
                )
                return True
            else:
                logger.error("Order failed: %s", result)
                return False
        except Exception as e:
            logger.error("Failed to open position: %s", e)
            return False

    def close_position(self, price: float) -> bool:
        """Close the current position."""
        if self.current_position is None:
            return False

        pos = self.current_position
        try:
            if pos.side == "long":
                result = self.client.close_long(self.symbol, pos.quantity)
            else:
                result = self.client.close_short(self.symbol, pos.quantity)

            if result.get("code") == 0:
                # Calculate PnL
                if pos.side == "long":
                    pnl = (price - pos.entry_price) * pos.quantity * self.leverage
                else:
                    pnl = (pos.entry_price - price) * pos.quantity * self.leverage

                self.risk.record_trade(pnl)
                logger.info(
                    "Closed %s position: qty=%.4f, entry=%.6f, exit=%.6f, PnL=%.4f USDT",
                    pos.side,
                    pos.quantity,
                    pos.entry_price,
                    price,
                    pnl,
                )
                self.current_position = None
                return True
            else:
                logger.error("Close order failed: %s", result)
                return False
        except Exception as e:
            logger.error("Failed to close position: %s", e)
            return False

    def tick(self) -> None:
        """Execute one cycle of the strategy."""
        price = self.get_current_price()
        if price is None:
            logger.warning("Could not fetch price, skipping tick")
            return

        logger.debug("Current USDC/USDT price: %.6f", price)

        # Check if we need to close existing position
        if self.current_position is not None:
            if self.should_close_position(price):
                self.close_position(price)
            return

        # Look for new entry signal
        signal = self.evaluate_signal(price)
        if signal:
            self.open_position(signal, price)
