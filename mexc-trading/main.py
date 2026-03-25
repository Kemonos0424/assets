"""
MEXC USDC/USDT Futures Trading Bot

Automated mean-reversion trading for the USDC/USDT stablecoin pair
using leveraged futures on MEXC exchange.

Usage:
    1. Copy .env.example to .env and fill in your MEXC API credentials
    2. pip install -r requirements.txt
    3. python main.py
"""
import logging
import signal
import sys
import time

from config import Config
from mexc_client import MEXCFuturesClient
from strategies.usdc_usdt_arb import USDCUSDTStrategy
from utils.risk_manager import RiskManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading.log"),
    ],
)
logger = logging.getLogger("main")

running = True


def shutdown_handler(signum, frame):
    """Handle graceful shutdown on SIGINT/SIGTERM."""
    global running
    logger.info("Shutdown signal received, closing positions and stopping...")
    running = False


def main():
    global running

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Validate configuration
    try:
        Config.validate()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("MEXC USDC/USDT Futures Trading Bot")
    logger.info("=" * 60)
    logger.info("Symbol:      %s", Config.SYMBOL)
    logger.info("Leverage:    %dx", Config.LEVERAGE)
    logger.info("Amount:      %.2f USDT", Config.TRADE_AMOUNT_USDT)
    logger.info("Threshold:   %.5f", Config.PRICE_DEVIATION_THRESHOLD)
    logger.info("Stop Loss:   %.1f%%", Config.STOP_LOSS_PERCENT)
    logger.info("Take Profit: %.1f%%", Config.TAKE_PROFIT_PERCENT)
    logger.info("Order Type:  %s (0%% maker fee)", Config.ORDER_TYPE)
    logger.info("Order Expiry: %ds", Config.ORDER_EXPIRY_SECONDS)
    logger.info("Interval:    %ds", Config.CHECK_INTERVAL_SECONDS)
    logger.info("=" * 60)

    # Initialize components
    client = MEXCFuturesClient()
    risk_manager = RiskManager()
    strategy = USDCUSDTStrategy(client, risk_manager)

    # Sync existing positions
    strategy.check_existing_positions()

    # Main trading loop
    logger.info("Trading bot started. Press Ctrl+C to stop.")
    last_daily_reset = time.strftime("%Y-%m-%d")

    while running:
        try:
            # Daily risk counter reset
            today = time.strftime("%Y-%m-%d")
            if today != last_daily_reset:
                risk_manager.reset_daily()
                last_daily_reset = today

            # Execute one strategy tick
            strategy.tick()

            # Wait for next interval
            time.sleep(Config.CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Unexpected error in main loop: %s", e, exc_info=True)
            time.sleep(30)  # Wait before retrying on error

    # Graceful shutdown - cancel pending orders and close positions
    if strategy.pending_order is not None:
        logger.info("Cancelling pending order before shutdown...")
        try:
            strategy.client.cancel_all_orders(strategy.symbol)
        except Exception:
            pass
        strategy.pending_order = None

    if strategy.current_position is not None:
        logger.info("Closing open position before shutdown...")
        price = strategy.get_current_price()
        if price:
            strategy._emergency_close(price)

    logger.info("Trading bot stopped.")


if __name__ == "__main__":
    main()
