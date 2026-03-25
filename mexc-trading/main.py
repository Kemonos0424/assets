"""
MEXC USDC/USDT Futures Trading Bot

Automated mean-reversion trading using post-only limit orders (0% maker fee)
with up to 200x leverage on MEXC exchange.

Modes:
    DRY_RUN=true  - Monitor prices and log signals without placing orders
    DRY_RUN=false - Live trading with real orders

Usage:
    1. Copy .env.example to .env and configure
    2. pip install -r requirements.txt
    3. python main.py              # dry-run mode (default)
    4. DRY_RUN=false python main.py  # live trading
"""
import logging
import signal
import sys
import time

import requests

from config import Config
from mexc_client import MEXCFuturesClient
from strategies.usdc_usdt_arb import USDCUSDTStrategy
from utils.risk_manager import RiskManager
from utils.notifier import Notifier

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
    global running
    logger.info("Shutdown signal received...")
    running = False


class DryRunClient:
    """Mock client that logs orders without sending them to the exchange."""

    def __init__(self):
        self.spot_url = Config.SPOT_BASE_URL
        self._order_counter = 0

    def get_ticker(self, symbol: str) -> dict:
        """Fetch real price data even in dry-run mode."""
        try:
            resp = requests.get(
                f"{self.spot_url}/api/v3/ticker/price",
                params={"symbol": symbol.replace("_", "")},
                timeout=10,
            )
            data = resp.json()
            price = float(data.get("price", 0))
            return {"code": 0, "data": {"lastPrice": price}}
        except Exception as e:
            logger.error("Price fetch failed: %s", e)
            return {"code": -1, "data": {}}

    def get_depth(self, symbol: str, limit: int = 5) -> dict:
        try:
            resp = requests.get(
                f"{self.spot_url}/api/v3/depth",
                params={"symbol": symbol.replace("_", ""), "limit": limit},
                timeout=10,
            )
            data = resp.json()
            bids = [{"price": b[0], "vol": b[1]} for b in data.get("bids", [])[:limit]]
            asks = [{"price": a[0], "vol": a[1]} for a in data.get("asks", [])[:limit]]
            return {"code": 0, "data": {"bids": bids, "asks": asks}}
        except Exception:
            return {"code": -1, "data": {"bids": [], "asks": []}}

    def set_leverage(self, symbol, leverage, open_type=1):
        logger.info("[DRY-RUN] Set leverage %dx for %s", leverage, symbol)
        return {"code": 0}

    def get_account_assets(self):
        return {"code": 0, "data": [{"currency": "USDT", "availableBalance": "1000"}]}

    def get_positions(self, symbol=None):
        return {"code": 0, "data": []}

    def _mock_order(self, action: str, symbol: str, price: float, vol: float, **kwargs) -> dict:
        self._order_counter += 1
        oid = f"dry-{self._order_counter}"
        logger.info("[DRY-RUN] %s: %s %.4f @ %.6f (order=%s)", action, symbol, vol, price, oid)
        return {"code": 0, "data": oid}

    def open_long_limit(self, symbol, price, vol, leverage):
        return self._mock_order("OPEN LONG LIMIT", symbol, price, vol)

    def open_short_limit(self, symbol, price, vol, leverage):
        return self._mock_order("OPEN SHORT LIMIT", symbol, price, vol)

    def close_long_limit(self, symbol, price, vol):
        return self._mock_order("CLOSE LONG LIMIT", symbol, price, vol)

    def close_short_limit(self, symbol, price, vol):
        return self._mock_order("CLOSE SHORT LIMIT", symbol, price, vol)

    def close_long_market(self, symbol, vol):
        return self._mock_order("CLOSE LONG MARKET (SL)", symbol, 0, vol)

    def close_short_market(self, symbol, vol):
        return self._mock_order("CLOSE SHORT MARKET (SL)", symbol, 0, vol)

    def cancel_order(self, symbol, order_id):
        logger.info("[DRY-RUN] Cancel order %s", order_id)
        return {"code": 0}

    def cancel_all_orders(self, symbol):
        logger.info("[DRY-RUN] Cancel all orders for %s", symbol)
        return {"code": 0}

    def get_order_detail(self, symbol, order_id):
        # In dry-run, orders are "filled" immediately
        return {"code": 0, "data": {"state": 2}}


class DryRunStrategy(USDCUSDTStrategy):
    """Strategy wrapper that tracks simulated PnL in dry-run mode."""

    def __init__(self, client, risk_manager, notifier):
        super().__init__(client, risk_manager)
        self.notifier = notifier
        self.sim_balance = 1000.0
        self.sim_trades = 0
        self.sim_pnl = 0.0

    def _place_entry_order(self, signal, price):
        result = super()._place_entry_order(signal, price)
        if result:
            self.notifier.notify_entry(signal, self.trade_amount / price, price, "limit (dry-run)")
        return result

    def _emergency_close(self, price):
        if self.current_position:
            pos = self.current_position
            if pos.side == "long":
                pnl = (price - pos.entry_price) * pos.quantity * self.leverage
            else:
                pnl = (pos.entry_price - price) * pos.quantity * self.leverage
            self.sim_pnl += pnl
            self.sim_trades += 1
            self.sim_balance += pnl
            self.notifier.notify_stop_loss(pos.side, price, pnl)
            logger.info("[DRY-RUN] SIM P&L: %.4f, Balance: %.2f", pnl, self.sim_balance)
        return super()._emergency_close(price)


def main():
    global running

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        Config.validate()
    except ValueError as e:
        logger.error("Config error: %s", e)
        sys.exit(1)

    mode = "DRY-RUN" if Config.DRY_RUN else "LIVE"
    notifier = Notifier()

    logger.info("=" * 60)
    logger.info("MEXC USDC/USDT Futures Trading Bot")
    logger.info("=" * 60)
    logger.info("Mode:        %s", mode)
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

    if Config.DRY_RUN:
        logger.info("*** DRY-RUN MODE: No real orders will be placed ***")
        logger.info("*** Real MEXC price data is used for simulation ***")
        logger.info("*** Set DRY_RUN=false in .env for live trading  ***")
        client = DryRunClient()
    else:
        logger.warning("*** LIVE TRADING MODE - Real money at risk ***")
        client = MEXCFuturesClient()

    risk_manager = RiskManager()

    if Config.DRY_RUN:
        strategy = DryRunStrategy(client, risk_manager, notifier)
    else:
        strategy = USDCUSDTStrategy(client, risk_manager)

    strategy.check_existing_positions()

    notifier.notify_start({
        "symbol": Config.SYMBOL,
        "leverage": Config.LEVERAGE,
        "mode": mode,
        "order_type": Config.ORDER_TYPE,
    })

    logger.info("Bot started. Press Ctrl+C to stop.")
    last_daily_reset = time.strftime("%Y-%m-%d")
    consecutive_errors = 0
    max_consecutive_errors = 10

    while running:
        try:
            # Daily reset
            today = time.strftime("%Y-%m-%d")
            if today != last_daily_reset:
                balance = 0.0
                if Config.DRY_RUN and isinstance(strategy, DryRunStrategy):
                    balance = strategy.sim_balance
                    notifier.notify_daily_report(
                        risk_manager.daily_pnl, risk_manager.trade_count, balance)
                else:
                    balance = strategy.get_account_balance()
                    notifier.notify_daily_report(
                        risk_manager.daily_pnl, risk_manager.trade_count, balance)
                risk_manager.reset_daily()
                last_daily_reset = today

            # Execute strategy tick
            strategy.tick()
            consecutive_errors = 0

            # Sim status (dry-run only, every 60 ticks)
            if Config.DRY_RUN and isinstance(strategy, DryRunStrategy):
                if strategy.sim_trades > 0 and strategy.sim_trades % 10 == 0:
                    logger.info(
                        "[SIM] Trades: %d, PnL: %.4f, Balance: %.2f",
                        strategy.sim_trades, strategy.sim_pnl, strategy.sim_balance,
                    )

            time.sleep(Config.CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            break
        except requests.exceptions.ConnectionError:
            consecutive_errors += 1
            wait = min(2 ** consecutive_errors, 60)
            logger.warning("Connection lost. Retrying in %ds... (%d/%d)",
                           wait, consecutive_errors, max_consecutive_errors)
            if consecutive_errors >= max_consecutive_errors:
                notifier.notify_error(
                    f"Connection lost {max_consecutive_errors} times. Stopping.")
                break
            time.sleep(wait)
        except Exception as e:
            consecutive_errors += 1
            logger.error("Error in main loop: %s", e, exc_info=True)
            notifier.notify_error(str(e))
            if consecutive_errors >= max_consecutive_errors:
                break
            time.sleep(min(2 ** consecutive_errors, 60))

    # Graceful shutdown
    if strategy.pending_order is not None:
        logger.info("Cancelling pending orders...")
        try:
            client.cancel_all_orders(strategy.symbol)
        except Exception:
            pass
        strategy.pending_order = None

    if strategy.current_position is not None:
        logger.info("Closing open position...")
        price = strategy.get_current_price()
        if price:
            strategy._emergency_close(price)

    balance = 0.0
    if Config.DRY_RUN and isinstance(strategy, DryRunStrategy):
        balance = strategy.sim_balance
        logger.info("=" * 60)
        logger.info("[DRY-RUN RESULTS]")
        logger.info("Total Trades: %d", strategy.sim_trades)
        logger.info("Total PnL:    %.4f USDT", strategy.sim_pnl)
        logger.info("Final Balance: %.2f USDT", strategy.sim_balance)
        logger.info("=" * 60)

    notifier.notify_stop("User shutdown" if not running else "Error limit", balance)
    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
