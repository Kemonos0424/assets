"""
Detailed backtesting with optimized parameters for USDC/USDT stablecoin arbitrage.

Since USDC/USDT has extremely tight price range (~0.0011), we need:
- Very tight entry thresholds (0.0001 - 0.0003)
- Short holding periods (mean reversion is fast for stablecoins)
- Higher leverage to amplify small moves
- 1-minute candles for maximum granularity
"""
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests


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
    holding_minutes: int = 0


@dataclass
class DailyPnL:
    date: str
    pnl: float
    trades: int
    balance: float


def fetch_klines(symbol: str, interval: str, days: int) -> list[dict]:
    """Fetch klines from MEXC spot API."""
    url = "https://api.mexc.com/api/v3/klines"
    all_klines = []
    end_time = int(time.time() * 1000)
    start_time = int((time.time() - days * 86400) * 1000)
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
                    "time_str": datetime.fromtimestamp(k[0] / 1000).strftime("%Y-%m-%d %H:%M"),
                })
            last_ts = data[-1][0]
            if last_ts <= current_start:
                break
            current_start = last_ts + 1
            time.sleep(0.15)
        except Exception as e:
            print(f"  Fetch error: {e}")
            break

    return all_klines


def run_backtest(
    klines: list[dict],
    leverage: int = 10,
    threshold: float = 0.0002,
    trade_amount: float = 100.0,
    stop_loss_pct: float = 0.005,
    take_profit_pct: float = 0.003,
    fair_value: float = 1.0,
    taker_fee: float = 0.0006,
    use_dynamic_fair_value: bool = True,
    ma_period: int = 96,  # ~1.5 hours of 1-min candles
) -> dict:
    """Run backtest with given parameters."""
    initial_balance = 1000.0
    balance = initial_balance
    peak_balance = initial_balance
    max_drawdown = 0.0
    trades: list[Trade] = []
    daily_pnls: list[DailyPnL] = []

    position_side = None
    entry_price = 0.0
    quantity = 0.0
    entry_time = ""
    entry_idx = 0
    stop_loss = 0.0
    take_profit = 0.0

    prev_day = None
    day_pnl = 0.0
    day_trades = 0
    daily_returns = []
    day_start_balance = balance

    # Precompute moving average for dynamic fair value
    prices = [k["close"] for k in klines]

    for i, candle in enumerate(klines):
        price = candle["close"]
        high = candle["high"]
        low = candle["low"]
        ts = candle["time_str"]

        # Daily tracking
        current_day = ts[:10]
        if prev_day and current_day != prev_day:
            daily_pnls.append(DailyPnL(prev_day, round(day_pnl, 4), day_trades, round(balance, 2)))
            daily_ret = (balance - day_start_balance) / day_start_balance if day_start_balance > 0 else 0
            daily_returns.append(daily_ret)
            day_pnl = 0.0
            day_trades = 0
            day_start_balance = balance
        prev_day = current_day

        # Dynamic fair value using moving average
        if use_dynamic_fair_value and i >= ma_period:
            fv = sum(prices[i - ma_period : i]) / ma_period
        else:
            fv = fair_value

        # --- Exit logic ---
        if position_side is not None:
            should_close = False
            exit_price = price

            if position_side == "long":
                if low <= stop_loss:
                    should_close = True
                    exit_price = stop_loss
                elif high >= take_profit:
                    should_close = True
                    exit_price = take_profit
                elif price >= fv:
                    should_close = True
                    exit_price = price
            else:
                if high >= stop_loss:
                    should_close = True
                    exit_price = stop_loss
                elif low <= take_profit:
                    should_close = True
                    exit_price = take_profit
                elif price <= fv:
                    should_close = True
                    exit_price = price

            if should_close:
                fee = quantity * exit_price * taker_fee
                if position_side == "long":
                    raw_pnl = (exit_price - entry_price) * quantity
                else:
                    raw_pnl = (entry_price - exit_price) * quantity

                leveraged_pnl = raw_pnl * leverage - fee
                pnl_pct = (leveraged_pnl / (quantity * entry_price)) * 100

                balance += leveraged_pnl
                peak_balance = max(peak_balance, balance)
                dd = (peak_balance - balance) / peak_balance * 100
                max_drawdown = max(max_drawdown, dd)

                holding_mins = (i - entry_idx)  # Each candle = interval minutes

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
                    holding_minutes=holding_mins,
                ))
                day_pnl += leveraged_pnl
                day_trades += 1
                position_side = None
                continue

        # --- Entry logic ---
        if position_side is None and balance > trade_amount * 0.5:
            deviation = price - fv

            signal = None
            if deviation < -threshold:
                signal = "long"
            elif deviation > threshold:
                signal = "short"

            if signal:
                quantity = trade_amount / price
                entry_price = price
                entry_time = ts
                entry_idx = i
                position_side = signal

                if signal == "long":
                    stop_loss = price * (1 - stop_loss_pct / leverage)
                    take_profit = price * (1 + take_profit_pct / leverage)
                else:
                    stop_loss = price * (1 + stop_loss_pct / leverage)
                    take_profit = price * (1 - take_profit_pct / leverage)

    # Close remaining position
    if position_side is not None and klines:
        last_price = klines[-1]["close"]
        fee = quantity * last_price * taker_fee
        if position_side == "long":
            raw_pnl = (last_price - entry_price) * quantity
        else:
            raw_pnl = (entry_price - last_price) * quantity
        leveraged_pnl = raw_pnl * leverage - fee
        balance += leveraged_pnl
        trades.append(Trade(
            side=position_side, entry_price=entry_price, exit_price=last_price,
            quantity=quantity, entry_time=entry_time, exit_time=klines[-1]["time_str"],
            pnl=leveraged_pnl, pnl_pct=(leveraged_pnl / (quantity * entry_price)) * 100,
            fees=fee, holding_minutes=len(klines) - entry_idx,
        ))

    # Final day
    if prev_day:
        daily_pnls.append(DailyPnL(prev_day, round(day_pnl, 4), day_trades, round(balance, 2)))
        if day_start_balance > 0:
            daily_returns.append((balance - day_start_balance) / day_start_balance)

    winning = [t for t in trades if t.pnl > 0]
    losing = [t for t in trades if t.pnl <= 0]
    total_fees = sum(t.fees for t in trades)

    sharpe = 0.0
    if len(daily_returns) > 1:
        avg_r = statistics.mean(daily_returns)
        std_r = statistics.stdev(daily_returns)
        if std_r > 0:
            sharpe = (avg_r / std_r) * (365 ** 0.5)

    return {
        "initial": initial_balance,
        "final": round(balance, 2),
        "return_pct": round((balance - initial_balance) / initial_balance * 100, 4),
        "total_trades": len(trades),
        "winning": len(winning),
        "losing": len(losing),
        "win_rate": round(len(winning) / len(trades) * 100, 1) if trades else 0,
        "max_drawdown": round(max_drawdown, 4),
        "avg_pnl": round(statistics.mean([t.pnl for t in trades]), 4) if trades else 0,
        "best_pnl": round(max(t.pnl for t in trades), 4) if trades else 0,
        "worst_pnl": round(min(t.pnl for t in trades), 4) if trades else 0,
        "total_fees": round(total_fees, 4),
        "sharpe": round(sharpe, 2),
        "avg_holding": round(statistics.mean([t.holding_minutes for t in trades]), 1) if trades else 0,
        "trades": trades,
        "daily_pnls": daily_pnls,
        "leverage": leverage,
        "threshold": threshold,
    }


def main():
    print("=" * 70)
    print("  MEXC USDC/USDT Futures Backtest - Detailed Analysis")
    print("=" * 70)

    # Fetch 5-minute candles for 30 days (more granular)
    print("\n  Fetching 5-minute candle data (30 days)...")
    klines = fetch_klines("USDCUSDT", "5m", 30)
    print(f"  Fetched: {len(klines)} candles")

    if not klines:
        print("  ERROR: No data fetched")
        sys.exit(1)

    prices = [k["close"] for k in klines]
    print(f"\n  [Market Data Summary]")
    print(f"  Period:      {klines[0]['time_str']} ~ {klines[-1]['time_str']}")
    print(f"  Price Range: {min(prices):.6f} ~ {max(prices):.6f}")
    print(f"  Avg Price:   {statistics.mean(prices):.6f}")
    print(f"  Std Dev:     {statistics.stdev(prices):.6f}")
    print(f"  Volatility:  {statistics.stdev(prices)/statistics.mean(prices)*100:.4f}%")

    # Price distribution
    above = sum(1 for p in prices if p > 1.0)
    below = sum(1 for p in prices if p < 1.0)
    at_peg = sum(1 for p in prices if p == 1.0)
    print(f"  Above 1.0:   {above} ({above/len(prices)*100:.1f}%)")
    print(f"  Below 1.0:   {below} ({below/len(prices)*100:.1f}%)")
    print(f"  At 1.0:      {at_peg} ({at_peg/len(prices)*100:.1f}%)")

    # ===== SCENARIO 1: Conservative =====
    print("\n" + "=" * 70)
    print("  SCENARIO 1: Conservative (10x leverage)")
    print("=" * 70)
    r1 = run_backtest(klines, leverage=10, threshold=0.0001, trade_amount=100,
                      stop_loss_pct=0.005, take_profit_pct=0.003)
    print_result(r1)

    # ===== SCENARIO 2: Moderate =====
    print("\n" + "=" * 70)
    print("  SCENARIO 2: Moderate (25x leverage)")
    print("=" * 70)
    r2 = run_backtest(klines, leverage=25, threshold=0.0001, trade_amount=100,
                      stop_loss_pct=0.005, take_profit_pct=0.003)
    print_result(r2)

    # ===== SCENARIO 3: Aggressive =====
    print("\n" + "=" * 70)
    print("  SCENARIO 3: Aggressive (50x leverage)")
    print("=" * 70)
    r3 = run_backtest(klines, leverage=50, threshold=0.0001, trade_amount=100,
                      stop_loss_pct=0.005, take_profit_pct=0.003)
    print_result(r3)

    # ===== SCENARIO 4: Ultra Aggressive =====
    print("\n" + "=" * 70)
    print("  SCENARIO 4: Ultra Aggressive (100x leverage)")
    print("=" * 70)
    r4 = run_backtest(klines, leverage=100, threshold=0.0001, trade_amount=100,
                      stop_loss_pct=0.003, take_profit_pct=0.002)
    print_result(r4)

    # ===== PARAMETER GRID SEARCH =====
    print("\n" + "=" * 70)
    print("  PARAMETER OPTIMIZATION GRID")
    print("=" * 70)
    print(f"\n  {'Lev':>5} {'Thresh':>10} {'SL%':>6} {'TP%':>6} | {'Trades':>6} {'Win%':>6} {'Return':>10} {'MaxDD':>8} {'Sharpe':>7}")
    print(f"  {'-'*5} {'-'*10} {'-'*6} {'-'*6} | {'-'*6} {'-'*6} {'-'*10} {'-'*8} {'-'*7}")

    best = None
    best_return = -999

    for lev in [10, 25, 50, 75, 100]:
        for thresh in [0.00005, 0.0001, 0.00015, 0.0002]:
            for sl in [0.003, 0.005]:
                for tp in [0.002, 0.003]:
                    r = run_backtest(klines, leverage=lev, threshold=thresh,
                                     stop_loss_pct=sl, take_profit_pct=tp)
                    m = "+" if r["return_pct"] >= 0 else ""
                    print(
                        f"  {lev:>4}x {thresh:>10.5f} {sl:>5.3f} {tp:>5.3f} | "
                        f"{r['total_trades']:>6} {r['win_rate']:>5.1f}% "
                        f"{m}{r['return_pct']:>9.3f}% {r['max_drawdown']:>7.3f}% {r['sharpe']:>6.2f}"
                    )
                    if r["return_pct"] > best_return:
                        best_return = r["return_pct"]
                        best = r

    if best:
        print(f"\n  [BEST PARAMETERS]")
        print(f"  Leverage:   {best['leverage']}x")
        print(f"  Threshold:  {best['threshold']:.5f}")
        print(f"  Return:     {best['return_pct']:.4f}%")
        print(f"  Trades:     {best['total_trades']}")
        print(f"  Win Rate:   {best['win_rate']:.1f}%")
        print(f"  Max DD:     {best['max_drawdown']:.4f}%")
        print(f"  Sharpe:     {best['sharpe']:.2f}")

    # Daily P&L for best scenario
    if best and best["daily_pnls"]:
        print(f"\n  [Daily P&L - Best Config]")
        print(f"  {'Date':<12} {'P&L':>10} {'Trades':>8} {'Balance':>12}")
        print(f"  {'-'*12} {'-'*10} {'-'*8} {'-'*12}")
        for d in best["daily_pnls"]:
            m = "+" if d.pnl >= 0 else ""
            print(f"  {d.date:<12} {m}{d.pnl:>9.4f} {d.trades:>8} {d.balance:>12.2f}")

    print("\n" + "=" * 70)
    print("  NOTE: Past performance does not guarantee future results.")
    print("  Futures trading with leverage carries significant risk.")
    print("=" * 70)


def print_result(r: dict) -> None:
    pnl = r["final"] - r["initial"]
    m = "+" if pnl >= 0 else ""
    print(f"  Balance:    {r['initial']:.2f} -> {r['final']:.2f} USDT ({m}{pnl:.2f})")
    print(f"  Return:     {m}{r['return_pct']:.4f}%")
    print(f"  Trades:     {r['total_trades']} (Win: {r['winning']}, Loss: {r['losing']})")
    print(f"  Win Rate:   {r['win_rate']:.1f}%")
    print(f"  Avg P&L:    {r['avg_pnl']:.4f} USDT")
    print(f"  Best/Worst: {r['best_pnl']:.4f} / {r['worst_pnl']:.4f} USDT")
    print(f"  Max DD:     {r['max_drawdown']:.4f}%")
    print(f"  Fees:       {r['total_fees']:.4f} USDT")
    print(f"  Sharpe:     {r['sharpe']:.2f}")
    print(f"  Avg Hold:   {r['avg_holding']:.0f} candles")

    if r["trades"]:
        print(f"\n  [Sample Trades (last 10)]")
        print(f"  {'Entry Time':<18} {'Side':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10}")
        print(f"  {'-'*16}  {'-'*4}  {'-'*10} {'-'*10} {'-'*10}")
        for t in r["trades"][-10:]:
            tm = "+" if t.pnl >= 0 else ""
            print(f"  {t.entry_time:<18} {t.side:<6} {t.entry_price:>10.6f} {t.exit_price:>10.6f} {tm}{t.pnl:>9.4f}")


if __name__ == "__main__":
    main()
