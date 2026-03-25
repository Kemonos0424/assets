"""Risk management module for futures trading."""
import logging

from config import Config

logger = logging.getLogger(__name__)


class RiskManager:
    """Manages trading risk with stop-loss, take-profit, and position limits."""

    def __init__(self):
        self.max_position_size = Config.MAX_POSITION_SIZE
        self.stop_loss_pct = Config.STOP_LOSS_PERCENT / 100
        self.take_profit_pct = Config.TAKE_PROFIT_PERCENT / 100
        self.leverage = Config.LEVERAGE
        self.daily_loss_limit = self.max_position_size * 0.1  # 10% of max position
        self.daily_pnl = 0.0
        self.trade_count = 0
        self.max_daily_trades = 5000  # ~5s interval * 5000 = ~7 hours of continuous trading

    def calculate_stop_loss(self, entry_price: float, side: str) -> float:
        """
        Calculate stop-loss price.

        For stablecoin pairs with high leverage, the stop-loss needs to be
        wider than the typical tick size to avoid false triggers.
        At 200x leverage, a 0.05% price move = 10% account move.
        We use a fixed absolute distance instead of leverage-adjusted percentage
        to ensure it's wider than normal market noise.
        """
        # Minimum SL distance: 0.05% of price (~5 ticks on USDC/USDT)
        min_sl_distance = entry_price * 0.0005
        # Leverage-adjusted SL
        leverage_sl_distance = entry_price * self.stop_loss_pct / self.leverage

        # Use the WIDER of the two to avoid premature triggers
        sl_distance = max(min_sl_distance, leverage_sl_distance)

        if side == "long":
            return entry_price - sl_distance
        return entry_price + sl_distance

    def calculate_take_profit(self, entry_price: float, side: str) -> float:
        """Calculate take-profit price."""
        # Minimum TP distance: 0.03% of price (~3 ticks)
        min_tp_distance = entry_price * 0.0003
        leverage_tp_distance = entry_price * self.take_profit_pct / self.leverage
        tp_distance = max(min_tp_distance, leverage_tp_distance)

        if side == "long":
            return entry_price + tp_distance
        return entry_price - tp_distance

    def calculate_position_size(
        self, balance: float, price: float, leverage: int
    ) -> float:
        """Calculate safe position size based on available balance."""
        max_by_balance = balance * leverage * 0.5  # Use max 50% of available margin
        max_by_limit = self.max_position_size
        position_value = min(max_by_balance, max_by_limit)
        quantity = position_value / price
        return round(quantity, 4)

    def can_open_trade(self, current_position_value: float) -> bool:
        """Check if a new trade is allowed based on risk limits."""
        if self.trade_count >= self.max_daily_trades:
            logger.warning("Daily trade limit reached: %d", self.max_daily_trades)
            return False

        if self.daily_pnl <= -self.daily_loss_limit:
            logger.warning(
                "Daily loss limit reached: %.2f / -%.2f",
                self.daily_pnl,
                self.daily_loss_limit,
            )
            return False

        if current_position_value >= self.max_position_size:
            logger.warning(
                "Max position size reached: %.2f / %.2f",
                current_position_value,
                self.max_position_size,
            )
            return False

        return True

    def record_trade(self, pnl: float) -> None:
        """Record a completed trade's PnL."""
        self.daily_pnl += pnl
        self.trade_count += 1
        logger.info(
            "Trade recorded - PnL: %.4f, Daily PnL: %.4f, Trades: %d",
            pnl,
            self.daily_pnl,
            self.trade_count,
        )

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        self.daily_pnl = 0.0
        self.trade_count = 0
        logger.info("Daily risk counters reset")
