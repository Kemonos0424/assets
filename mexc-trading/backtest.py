"""
Backtesting simulator for USDC/USDT mean-reversion strategy.

Fetches real historical kline data from MEXC public API and simulates
the trading strategy with configurable leverage and risk parameters.
"""
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests


# --- Configuration ---
SYMBOL = "USDCUSDT"
LEVERAGE = 10
INITIAL_BALANCE = 1000.0  # USDT
TRADE_AMOUNT = 100.0  # USDT per trade
PRICE_DEVIATION_THRESHOLD = 0.0005  # 0.05% from peg (tighter for stablecoin)
STOP_LOSS_PCT = 0.02  # 2%
TAKE_PROFIT_PCT = 0.03  # 3%
FAIR_VALUE = 1.0000
MAKER_FEE = 0.0002  # 0.02%
TAKER_FEE = 0.0006  # 0.06%


@dataclass
class Trade:
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    entry_time: str
    exit_time: str
    pnl: float
    pnl_pct: float
    fees: float


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    max_drawdown_pct: float
    avg_trade_pnl: float
    best_trade_pnl: float
    worst_trade_pnl: float
    total_fees: float
    sharpe_ratio: float
    trades: list = field(default_factory=list)


def fetch_klines_spot(symbol: str, interval: str, days: int) -> list[dict]:
    """Fetch historical kline data from MEXC Spot API v3."""
    url = "https://api.mexc.com/api/v3/klines"
    all_klines = []

    end_time = int(time.time() * 1000)
    start_time = int((time.time() - days * 86400) * 1000)

    # MEXC returns max 1000 candles per request
    current_start = start_time
    while current_start < end_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_time,
            "limit": 1000,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            for k in data:
                all_klines.append({
                    "timestamp": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "time_str": datetime.fromtimestamp(k[0] / 1000).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                })

            # Move to next batch
            last_ts = data[-1][0]
            if last_ts <= current_start:
                break
            current_start = last_ts + 1

            time.sleep(0.2)  # Rate limit
        except Exception as e:
            print(f"Error fetching klines: {e}")
            break

    return all_klines


def run_backtest(klines: list[dict]) -> BacktestResult:
    """Run the mean-reversion backtest on historical data."""
    balance = INITIAL_BALANCE
    peak_balance = INITIAL_BALANCE
    max_drawdown = 0.0
    trades: list[Trade] = []
    daily_returns: list[float] = []

    # Position state
    position_side = None  # "long" or "short"
    entry_price = 0.0
    quantity = 0.0
    entry_time = ""
    stop_loss = 0.0
    take_profit = 0.0

    prev_day = None
    day_start_balance = balance

    for candle in klines:
        price = candle["close"]
        high = candle["high"]
        low = candle["low"]
        ts = candle["time_str"]

        # Track daily returns
        current_day = ts[:10]
        if prev_day and current_day != prev_day:
            daily_ret = (balance - day_start_balance) / day_start_balance
            daily_returns.append(daily_ret)
            day_start_balance = balance
        prev_day = current_day

        # --- Check exit conditions for open position ---
        if position_side is not None:
            should_close = False
            exit_price = price

            if position_side == "long":
                # Check stop-loss (using low of candle)
                if low <= stop_loss:
                    should_close = True
                    exit_price = stop_loss
                # Check take-profit (using high of candle)
                elif high >= take_profit:
                    should_close = True
                    exit_price = take_profit
                # Mean reversion - close at fair value
                elif price >= FAIR_VALUE:
                    should_close = True
                    exit_price = price

            elif position_side == "short":
                if high >= stop_loss:
                    should_close = True
                    exit_price = stop_loss
                elif low <= take_profit:
                    should_close = True
                    exit_price = take_profit
                elif price <= FAIR_VALUE:
                    should_close = True
                    exit_price = price

            if should_close:
                # Calculate PnL
                fee = quantity * exit_price * TAKER_FEE
                if position_side == "long":
                    raw_pnl = (exit_price - entry_price) * quantity
                else:
                    raw_pnl = (entry_price - exit_price) * quantity

                leveraged_pnl = raw_pnl * LEVERAGE - fee
                pnl_pct = (leveraged_pnl / (quantity * entry_price)) * 100

                balance += leveraged_pnl
                peak_balance = max(peak_balance, balance)
                drawdown = (peak_balance - balance) / peak_balance * 100
                max_drawdown = max(max_drawdown, drawdown)

                trades.append(Trade(
                    side=position_side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=quantity,
                    entry_time=entry_time,
                    exit_time=ts,
                    pnl=leveraged_pnl,
                    pnl_pct=pnl_pct,
                    fees=fee,
                ))

                position_side = None
                continue

        # --- Check entry conditions (no position) ---
        if position_side is None:
            deviation = price - FAIR_VALUE
            signal = None

            if deviation < -PRICE_DEVIATION_THRESHOLD:
                signal = "long"
            elif deviation > PRICE_DEVIATION_THRESHOLD:
                signal = "short"

            if signal and balance > TRADE_AMOUNT:
                quantity = TRADE_AMOUNT / price
                entry_price = price
                entry_time = ts
                position_side = signal
                fee = quantity * price * TAKER_FEE

                if signal == "long":
                    stop_loss = price * (1 - STOP_LOSS_PCT / LEVERAGE)
                    take_profit = price * (1 + TAKE_PROFIT_PCT / LEVERAGE)
                else:
                    stop_loss = price * (1 + STOP_LOSS_PCT / LEVERAGE)
                    take_profit = price * (1 - TAKE_PROFIT_PCT / LEVERAGE)

    # Close any remaining position at last price
    if position_side is not None and klines:
        last_price = klines[-1]["close"]
        fee = quantity * last_price * TAKER_FEE
        if position_side == "long":
            raw_pnl = (last_price - entry_price) * quantity
        else:
            raw_pnl = (entry_price - last_price) * quantity
        leveraged_pnl = raw_pnl * LEVERAGE - fee
        balance += leveraged_pnl
        trades.append(Trade(
            side=position_side,
            entry_price=entry_price,
            exit_price=last_price,
            quantity=quantity,
            entry_time=entry_time,
            exit_time=klines[-1]["time_str"],
            pnl=leveraged_pnl,
            pnl_pct=(leveraged_pnl / (quantity * entry_price)) * 100,
            fees=fee,
        ))

    # Calculate statistics
    winning = [t for t in trades if t.pnl > 0]
    losing = [t for t in trades if t.pnl <= 0]
    total_fees = sum(t.fees for t in trades)

    # Sharpe ratio (annualized)
    if daily_returns and len(daily_returns) > 1:
        import statistics
        avg_ret = statistics.mean(daily_returns)
        std_ret = statistics.stdev(daily_returns)
        sharpe = (avg_ret / std_ret) * (365 ** 0.5) if std_ret > 0 else 0
    else:
        sharpe = 0.0

    return BacktestResult(
        initial_balance=INITIAL_BALANCE,
        final_balance=round(balance, 2),
        total_return_pct=round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        total_trades=len(trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        win_rate=round(len(winning) / len(trades) * 100, 1) if trades else 0,
        max_drawdown_pct=round(max_drawdown, 2),
        avg_trade_pnl=round(sum(t.pnl for t in trades) / len(trades), 4) if trades else 0,
        best_trade_pnl=round(max(t.pnl for t in trades), 4) if trades else 0,
        worst_trade_pnl=round(min(t.pnl for t in trades), 4) if trades else 0,
        total_fees=round(total_fees, 4),
        sharpe_ratio=round(sharpe, 2),
        trades=trades,
    )


def print_results(result: BacktestResult, klines: list[dict]) -> None:
    """Print formatted backtest results."""
    print("\n" + "=" * 70)
    print("  MEXC USDC/USDT Futures Backtest Results")
    print("=" * 70)

    if klines:
        print(f"  Period:       {klines[0]['time_str']} ~ {klines[-1]['time_str']}")
        print(f"  Data Points:  {len(klines)} candles (15min)")
    print(f"  Leverage:     {LEVERAGE}x")
    print(f"  Threshold:    +/- {PRICE_DEVIATION_THRESHOLD * 100:.3f}%")
    print(f"  Trade Size:   {TRADE_AMOUNT} USDT")
    print("-" * 70)

    # Price statistics
    if klines:
        prices = [k["close"] for k in klines]
        print(f"\n  [Price Statistics]")
        print(f"  Min Price:    {min(prices):.6f}")
        print(f"  Max Price:    {max(prices):.6f}")
        print(f"  Avg Price:    {sum(prices)/len(prices):.6f}")
        print(f"  Price Range:  {(max(prices)-min(prices)):.6f}")

    # Performance
    pnl = result.final_balance - result.initial_balance
    print(f"\n  [Performance]")
    print(f"  Initial:      {result.initial_balance:.2f} USDT")
    print(f"  Final:        {result.final_balance:.2f} USDT")
    print(f"  P&L:          {'+' if pnl >= 0 else ''}{pnl:.2f} USDT")
    print(f"  Return:       {'+' if result.total_return_pct >= 0 else ''}{result.total_return_pct:.2f}%")
    print(f"  Max Drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe Ratio: {result.sharpe_ratio:.2f}")

    # Trading stats
    print(f"\n  [Trading Statistics]")
    print(f"  Total Trades: {result.total_trades}")
    print(f"  Win / Loss:   {result.winning_trades} / {result.losing_trades}")
    print(f"  Win Rate:     {result.win_rate:.1f}%")
    print(f"  Avg P&L:      {result.avg_trade_pnl:.4f} USDT")
    print(f"  Best Trade:   {result.best_trade_pnl:.4f} USDT")
    print(f"  Worst Trade:  {result.worst_trade_pnl:.4f} USDT")
    print(f"  Total Fees:   {result.total_fees:.4f} USDT")

    # Recent trades
    if result.trades:
        print(f"\n  [Recent Trades (last 20)]")
        print(f"  {'Time':<18} {'Side':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'P&L%':>8}")
        print(f"  {'-'*16}  {'-'*4}  {'-'*10} {'-'*10} {'-'*10} {'-'*8}")
        for t in result.trades[-20:]:
            marker = "+" if t.pnl >= 0 else ""
            print(
                f"  {t.entry_time:<18} {t.side:<6} {t.entry_price:>10.6f} "
                f"{t.exit_price:>10.6f} {marker}{t.pnl:>9.4f} {marker}{t.pnl_pct:>7.2f}%"
            )

    # Sensitivity analysis header
    print(f"\n  [Leverage Sensitivity]")
    print(f"  {'Leverage':>10} {'Return':>12} {'Max DD':>10} {'Final':>12}")
    print(f"  {'-'*10} {'-'*12} {'-'*10} {'-'*12}")

    print("=" * 70)


def run_sensitivity(klines: list[dict]) -> None:
    """Run backtest with different leverage levels."""
    global LEVERAGE
    original_leverage = LEVERAGE

    for lev in [5, 10, 20, 50, 75, 100]:
        LEVERAGE = lev
        result = run_backtest(klines)
        pnl = result.final_balance - result.initial_balance
        marker = "+" if pnl >= 0 else ""
        print(
            f"  {lev:>8}x  {marker}{result.total_return_pct:>10.2f}%  "
            f"{result.max_drawdown_pct:>8.2f}%  {result.final_balance:>10.2f}"
        )

    LEVERAGE = original_leverage
    print("=" * 70)


def main():
    print("Fetching USDC/USDT historical data from MEXC (past 30 days)...")
    print("Using 15-minute candles for granular simulation.\n")

    # Fetch 30 days of 15-min candles
    klines = fetch_klines_spot(SYMBOL, "15m", 30)

    if not klines:
        print("ERROR: Could not fetch historical data. Trying with generated data...")
        sys.exit(1)

    print(f"Fetched {len(klines)} candles.")

    # Run main backtest
    result = run_backtest(klines)
    print_results(result, klines)

    # Leverage sensitivity analysis
    run_sensitivity(klines)

    # Threshold sensitivity
    global PRICE_DEVIATION_THRESHOLD
    original_threshold = PRICE_DEVIATION_THRESHOLD

    print(f"\n  [Threshold Sensitivity (Leverage={LEVERAGE}x)]")
    print(f"  {'Threshold':>12} {'Trades':>8} {'Win%':>8} {'Return':>12} {'Max DD':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*12} {'-'*10}")

    for thresh in [0.0002, 0.0005, 0.001, 0.0015, 0.002, 0.003]:
        PRICE_DEVIATION_THRESHOLD = thresh
        r = run_backtest(klines)
        marker = "+" if r.total_return_pct >= 0 else ""
        print(
            f"  {thresh:>10.4f}%  {r.total_trades:>6}  {r.win_rate:>6.1f}%  "
            f"{marker}{r.total_return_pct:>10.2f}%  {r.max_drawdown_pct:>8.2f}%"
        )

    PRICE_DEVIATION_THRESHOLD = original_threshold
    print("=" * 70)


if __name__ == "__main__":
    main()
