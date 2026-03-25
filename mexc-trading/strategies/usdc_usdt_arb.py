"""
USDC/USDT Futures Arbitrage Strategy - Limit Order (Maker) Mode.

Uses post-only limit orders for 0% maker fee and zero slippage.
With 200x leverage on USDC/USDT stablecoin pair, small deviations
from the 1.0 peg generate significant leveraged returns.

Order flow:
  1. Monitor price via ticker
  2. When deviation exceeds threshold, place post-only limit order at target price
  3. Monitor order status - cancel if not filled within expiry window
  4. When position is open, place limit order to close at fair value
  5. Emergency: use market order for stop-loss only
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


@dataclass
class PendingOrder:
    order_id: str
    side: str  # "long" or "short"
    price: float
    quantity: float
    created_at: float
    is_close: bool = False


class USDCUSDTStrategy:
    """Mean-reversion strategy using post-only limit orders for zero fees."""

    FAIR_VALUE = 1.0000

    def __init__(self, client: MEXCFuturesClient, risk_manager: RiskManager):
        self.client = client
        self.risk = risk_manager
        self.symbol = Config.SYMBOL
        self.leverage = Config.LEVERAGE
        self.threshold = Config.PRICE_DEVIATION_THRESHOLD
        self.trade_amount = Config.TRADE_AMOUNT_USDT
        self.order_expiry = Config.ORDER_EXPIRY_SECONDS
        self.current_position: Position | None = None
        self.pending_order: PendingOrder | None = None
        self._setup_leverage()

    def _setup_leverage(self) -> None:
        """Configure leverage on the exchange."""
        try:
            result = self.client.set_leverage(self.symbol, self.leverage)
            logger.info("Leverage set to %dx: %s", self.leverage, result)
        except Exception as e:
            logger.error("Failed to set leverage: %s", e)

    def get_current_price(self) -> float | None:
        """Fetch the current last price for USDC/USDT."""
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

    def get_best_bid_ask(self) -> tuple[float, float] | None:
        """Get best bid and ask from order book."""
        try:
            depth = self.client.get_depth(self.symbol, limit=5)
            data = depth.get("data", {})
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0].get("price", 0))
                best_ask = float(asks[0].get("price", 0))
                return best_bid, best_ask
            return None
        except Exception as e:
            logger.error("Failed to get order book: %s", e)
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
                        "Existing position: %s %.4f @ %.6f",
                        side, vol, self.current_position.entry_price,
                    )
                    return

            self.current_position = None
        except Exception as e:
            logger.error("Failed to check positions: %s", e)

    def _check_pending_order(self) -> bool:
        """
        Check if pending order has been filled, expired, or needs cancellation.
        Returns True if we should skip further processing this tick.
        """
        if self.pending_order is None:
            return False

        order = self.pending_order
        elapsed = time.time() - order.created_at

        try:
            result = self.client.get_order_detail(self.symbol, order.order_id)
            data = result.get("data", {})
            state = data.get("state", 0)

            # State: 2=filled, 3=partially filled, 4=cancelled, 5=partially cancelled
            if state == 2:
                # Fully filled
                logger.info("Order %s FILLED: %s @ %.6f",
                            order.order_id, order.side, order.price)

                if order.is_close:
                    # Close order filled - record PnL
                    if self.current_position:
                        pos = self.current_position
                        if pos.side == "long":
                            pnl = (order.price - pos.entry_price) * pos.quantity * self.leverage
                        else:
                            pnl = (pos.entry_price - order.price) * pos.quantity * self.leverage
                        self.risk.record_trade(pnl)
                        logger.info("Position closed. PnL: %.4f USDT", pnl)
                    self.current_position = None
                else:
                    # Entry order filled - create position
                    stop_loss = self.risk.calculate_stop_loss(order.price, order.side)
                    take_profit = self.risk.calculate_take_profit(order.price, order.side)
                    self.current_position = Position(
                        side=order.side,
                        entry_price=order.price,
                        quantity=order.quantity,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        open_time=time.time(),
                    )
                    logger.info(
                        "Position opened: %s %.4f @ %.6f, SL=%.6f, TP=%.6f",
                        order.side, order.quantity, order.price, stop_loss, take_profit,
                    )

                self.pending_order = None
                return True

            if state in (4, 5):
                # Cancelled
                logger.info("Order %s cancelled", order.order_id)
                self.pending_order = None
                return False

            # Still open - check expiry
            if elapsed > self.order_expiry:
                logger.info("Order %s expired (%.0fs), cancelling", order.order_id, elapsed)
                self.client.cancel_order(self.symbol, order.order_id)
                self.pending_order = None
                return False

            # Still waiting
            return True

        except Exception as e:
            logger.error("Failed to check order %s: %s", order.order_id, e)
            if elapsed > self.order_expiry * 2:
                self.pending_order = None
            return True

    def evaluate_signal(self, price: float) -> str | None:
        """Determine trading signal based on price deviation from peg."""
        deviation = price - self.FAIR_VALUE

        if deviation < -self.threshold:
            logger.info("LONG signal: price=%.6f, dev=%.6f", price, deviation)
            return "long"
        if deviation > self.threshold:
            logger.info("SHORT signal: price=%.6f, dev=%.6f", price, deviation)
            return "short"
        return None

    def _place_entry_order(self, signal: str, price: float) -> bool:
        """Place a post-only limit entry order."""
        balance = self.get_account_balance()
        quantity = self.risk.calculate_position_size(balance, price, self.leverage)

        if quantity <= 0:
            logger.warning("Insufficient balance for new position")
            return False

        if not self.risk.can_open_trade(quantity * price):
            return False

        # For post-only: place at current price to sit on the book
        # Long: place at bid (or slightly below ask)
        # Short: place at ask (or slightly above bid)
        bid_ask = self.get_best_bid_ask()
        if bid_ask:
            best_bid, best_ask = bid_ask
            if signal == "long":
                limit_price = best_bid  # Buy at bid = maker
            else:
                limit_price = best_ask  # Sell at ask = maker
        else:
            limit_price = price

        try:
            if signal == "long":
                result = self.client.open_long_limit(
                    self.symbol, limit_price, quantity, self.leverage)
            else:
                result = self.client.open_short_limit(
                    self.symbol, limit_price, quantity, self.leverage)

            if result.get("code") == 0:
                order_id = str(result.get("data", ""))
                self.pending_order = PendingOrder(
                    order_id=order_id,
                    side=signal,
                    price=limit_price,
                    quantity=quantity,
                    created_at=time.time(),
                    is_close=False,
                )
                logger.info(
                    "Limit entry placed: %s %.4f @ %.6f (order=%s)",
                    signal, quantity, limit_price, order_id,
                )
                return True
            else:
                logger.error("Limit entry failed: %s", result)
                return False
        except Exception as e:
            logger.error("Failed to place entry: %s", e)
            return False

    def _place_close_order(self, price: float) -> bool:
        """Place a post-only limit order to close position at fair value."""
        if self.current_position is None:
            return False

        pos = self.current_position

        # Close at fair value for mean reversion profit
        bid_ask = self.get_best_bid_ask()
        if bid_ask:
            best_bid, best_ask = bid_ask
            if pos.side == "long":
                # Sell at ask = maker
                close_price = best_ask
            else:
                # Buy at bid = maker
                close_price = best_bid
        else:
            close_price = self.FAIR_VALUE

        try:
            if pos.side == "long":
                result = self.client.close_long_limit(
                    self.symbol, close_price, pos.quantity)
            else:
                result = self.client.close_short_limit(
                    self.symbol, close_price, pos.quantity)

            if result.get("code") == 0:
                order_id = str(result.get("data", ""))
                self.pending_order = PendingOrder(
                    order_id=order_id,
                    side=pos.side,
                    price=close_price,
                    quantity=pos.quantity,
                    created_at=time.time(),
                    is_close=True,
                )
                logger.info(
                    "Limit close placed: %s %.4f @ %.6f (order=%s)",
                    pos.side, pos.quantity, close_price, order_id,
                )
                return True
            else:
                logger.error("Limit close failed: %s", result)
                return False
        except Exception as e:
            logger.error("Failed to place close order: %s", e)
            return False

    def _emergency_close(self, price: float) -> bool:
        """Emergency market order close for stop-loss situations."""
        if self.current_position is None:
            return False

        pos = self.current_position
        logger.warning("EMERGENCY CLOSE (stop-loss): %s @ %.6f", pos.side, price)

        # Cancel any pending close orders first
        if self.pending_order and self.pending_order.is_close:
            try:
                self.client.cancel_order(self.symbol, self.pending_order.order_id)
            except Exception:
                pass
            self.pending_order = None

        try:
            if pos.side == "long":
                result = self.client.close_long_market(self.symbol, pos.quantity)
            else:
                result = self.client.close_short_market(self.symbol, pos.quantity)

            if result.get("code") == 0:
                if pos.side == "long":
                    pnl = (price - pos.entry_price) * pos.quantity * self.leverage
                else:
                    pnl = (pos.entry_price - price) * pos.quantity * self.leverage
                self.risk.record_trade(pnl)
                logger.warning("Emergency close executed. PnL: %.4f USDT", pnl)
                self.current_position = None
                return True
            else:
                logger.error("Emergency close failed: %s", result)
                return False
        except Exception as e:
            logger.error("Emergency close error: %s", e)
            return False

    def _check_stop_loss(self, price: float) -> bool:
        """Check if stop-loss should be triggered (uses market order)."""
        if self.current_position is None:
            return False

        pos = self.current_position
        if pos.side == "long" and price <= pos.stop_loss:
            return True
        if pos.side == "short" and price >= pos.stop_loss:
            return True
        return False

    def tick(self) -> None:
        """Execute one cycle of the strategy."""
        # Step 1: Check pending order status
        if self._check_pending_order():
            return  # Still waiting for order fill

        price = self.get_current_price()
        if price is None:
            logger.warning("Could not fetch price, skipping tick")
            return

        logger.debug("USDC/USDT: %.6f", price)

        # Step 2: If we have a position, manage it
        if self.current_position is not None:
            # Check emergency stop-loss (market order)
            if self._check_stop_loss(price):
                self._emergency_close(price)
                return

            # Check if we should close at fair value (limit order)
            pos = self.current_position
            should_close = False
            if pos.side == "long" and price >= self.FAIR_VALUE:
                should_close = True
            elif pos.side == "short" and price <= self.FAIR_VALUE:
                should_close = True

            if should_close and self.pending_order is None:
                self._place_close_order(price)
            return

        # Step 3: Look for new entry signal
        signal = self.evaluate_signal(price)
        if signal:
            self._place_entry_order(signal, price)
