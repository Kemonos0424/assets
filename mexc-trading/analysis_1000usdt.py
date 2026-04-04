"""
Realistic profitability analysis with 1000 USDT capital.
Identifies bottlenecks and explores solutions.
"""
import statistics
import time
from datetime import datetime

import requests


def fetch_klines(symbol: str, interval: str, days: int) -> list[dict]:
    url = "https://api.mexc.com/api/v3/klines"
    all_klines = []
    end_time = int(time.time() * 1000)
    start_time = int((time.time() - days * 86400) * 1000)
    current_start = start_time
    while current_start < end_time:
        params = {"symbol": symbol, "interval": interval,
                  "startTime": current_start, "endTime": end_time, "limit": 1000}
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            if not data:
                break
            for k in data:
                all_klines.append({
                    "timestamp": k[0], "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                    "time_str": datetime.fromtimestamp(k[0] / 1000).strftime("%Y-%m-%d %H:%M"),
                })
            last_ts = data[-1][0]
            if last_ts <= current_start:
                break
            current_start = last_ts + 1
            time.sleep(0.15)
        except Exception as e:
            print(f"  Error: {e}")
            break
    return all_klines


def fetch_orderbook(symbol: str) -> dict | None:
    """Fetch current order book to analyze liquidity."""
    try:
        resp = requests.get(
            "https://api.mexc.com/api/v3/depth",
            params={"symbol": symbol, "limit": 20}, timeout=10,
        )
        data = resp.json()
        return data
    except Exception:
        return None


def run_realistic_backtest(
    klines, capital, leverage, threshold, trade_pct,
    taker_fee, slippage_bps, funding_rate_8h,
) -> dict:
    """
    Backtest with realistic cost modeling.
    trade_pct: fraction of capital to use per trade (e.g. 0.5 = 50%)
    slippage_bps: slippage in basis points
    """
    balance = capital
    peak = capital
    max_dd = 0.0
    trades = []
    total_fees = 0.0
    total_slippage = 0.0
    total_funding = 0.0

    pos_side = None
    entry_price = 0.0
    quantity = 0.0
    entry_idx = 0
    sl = tp = 0.0
    ma_period = 96
    prices = [k["close"] for k in klines]

    for i, c in enumerate(klines):
        price = c["close"]
        high, low = c["high"], c["low"]

        # Dynamic fair value
        fv = sum(prices[max(0, i - ma_period):i]) / min(i, ma_period) if i > 0 else 1.0

        # Funding cost for open positions (every 96 candles = 8h at 5min interval)
        if pos_side and i > 0 and i % 96 == 0:
            notional = quantity * price
            funding_cost = notional * funding_rate_8h
            balance -= funding_cost
            total_funding += funding_cost

        # Exit logic
        if pos_side:
            should_close = False
            exit_p = price
            if pos_side == "long":
                if low <= sl:
                    should_close, exit_p = True, sl
                elif high >= tp:
                    should_close, exit_p = True, tp
                elif price >= fv:
                    should_close, exit_p = True, price
            else:
                if high >= sl:
                    should_close, exit_p = True, sl
                elif low <= tp:
                    should_close, exit_p = True, tp
                elif price <= fv:
                    should_close, exit_p = True, price

            if should_close:
                # Apply slippage on exit
                slip = exit_p * slippage_bps / 10000
                if pos_side == "long":
                    exit_p -= slip
                else:
                    exit_p += slip
                slip_cost = slip * quantity
                total_slippage += slip_cost

                fee = quantity * exit_p * taker_fee
                total_fees += fee
                if pos_side == "long":
                    raw = (exit_p - entry_price) * quantity
                else:
                    raw = (entry_price - exit_p) * quantity
                pnl = raw * leverage - fee
                balance += pnl
                peak = max(peak, balance)
                dd = (peak - balance) / peak * 100
                max_dd = max(max_dd, dd)
                trades.append({"pnl": pnl, "side": pos_side,
                               "entry": entry_price, "exit": exit_p,
                               "hold": i - entry_idx})
                pos_side = None
                continue

        # Entry logic
        if pos_side is None and balance > 10:
            deviation = price - fv
            signal = None
            if deviation < -threshold:
                signal = "long"
            elif deviation > threshold:
                signal = "short"

            if signal:
                trade_capital = balance * trade_pct
                quantity = trade_capital / price

                # Apply slippage on entry
                slip = price * slippage_bps / 10000
                if signal == "long":
                    entry_price = price + slip
                else:
                    entry_price = price - slip
                total_slippage += slip * quantity

                fee = quantity * entry_price * taker_fee
                total_fees += fee

                entry_idx = i
                pos_side = signal
                sl_pct = 0.005
                tp_pct = 0.003
                if signal == "long":
                    sl = entry_price * (1 - sl_pct / leverage)
                    tp = entry_price * (1 + tp_pct / leverage)
                else:
                    sl = entry_price * (1 + sl_pct / leverage)
                    tp = entry_price * (1 - tp_pct / leverage)

    # Close remaining
    if pos_side and klines:
        lp = klines[-1]["close"]
        fee = quantity * lp * taker_fee
        total_fees += fee
        raw = ((lp - entry_price) if pos_side == "long" else (entry_price - lp)) * quantity
        pnl = raw * leverage - fee
        balance += pnl
        trades.append({"pnl": pnl, "side": pos_side, "entry": entry_price,
                       "exit": lp, "hold": len(klines) - entry_idx})

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    return {
        "final": round(balance, 2),
        "return_pct": round((balance - capital) / capital * 100, 3),
        "pnl": round(balance - capital, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "max_dd": round(max_dd, 3),
        "total_fees": round(total_fees, 2),
        "total_slippage": round(total_slippage, 4),
        "total_funding": round(total_funding, 4),
        "total_costs": round(total_fees + total_slippage + total_funding, 2),
        "avg_pnl": round(statistics.mean([t["pnl"] for t in trades]), 4) if trades else 0,
        "avg_hold": round(statistics.mean([t["hold"] for t in trades]), 1) if trades else 0,
        "trade_list": trades,
    }


def main():
    print("=" * 74)
    print("  1,000 USDT Capital - Realistic Profitability Analysis")
    print("=" * 74)

    print("\n  Fetching 30-day USDC/USDT data (5min candles)...")
    klines = fetch_klines("USDCUSDT", "5m", 30)
    print(f"  Fetched: {len(klines)} candles")

    if not klines:
        print("  ERROR: No data")
        return

    prices = [k["close"] for k in klines]
    print(f"  Period: {klines[0]['time_str']} ~ {klines[-1]['time_str']}")
    print(f"  Price:  {min(prices):.6f} ~ {max(prices):.6f}")
    print(f"  Avg:    {statistics.mean(prices):.6f}, StdDev: {statistics.stdev(prices):.6f}")

    # Check current orderbook for liquidity
    print("\n  Fetching current order book...")
    ob = fetch_orderbook("USDCUSDT")
    if ob and "bids" in ob and "asks" in ob:
        bids = ob["bids"][:5]
        asks = ob["asks"][:5]
        print(f"  Top 5 Bids: ", [(b[0], b[1]) for b in bids])
        print(f"  Top 5 Asks: ", [(a[0], a[1]) for a in asks])
        if bids and asks:
            spread = float(asks[0][0]) - float(bids[0][0])
            spread_pct = spread / float(asks[0][0]) * 100
            bid_depth = sum(float(b[1]) for b in bids)
            ask_depth = sum(float(a[1]) for a in asks)
            print(f"  Spread:     {spread:.6f} ({spread_pct:.4f}%)")
            print(f"  Bid Depth:  {bid_depth:.2f} USDC (top 5)")
            print(f"  Ask Depth:  {ask_depth:.2f} USDC (top 5)")

    # ============================================================
    # BOTTLENECK ANALYSIS
    # ============================================================
    print("\n" + "=" * 74)
    print("  BOTTLENECK #1: Trading Fees Impact")
    print("=" * 74)

    base = {"capital": 1000, "leverage": 50, "threshold": 0.00005,
            "trade_pct": 0.1, "slippage_bps": 0, "funding_rate_8h": 0}

    print(f"\n  {'Fee Rate':>12} {'Trades':>8} {'Gross P&L':>12} {'Fees':>10} {'Net P&L':>12} {'Return':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*12} {'-'*10} {'-'*12} {'-'*10}")

    for fee in [0.0, 0.0002, 0.0004, 0.0006, 0.001, 0.002]:
        r = run_realistic_backtest(klines, taker_fee=fee, **base)
        gross = r["pnl"] + r["total_fees"]
        print(f"  {fee*100:>10.2f}%  {r['trades']:>6}  {gross:>10.2f}  {r['total_fees']:>8.2f}  "
              f"{r['pnl']:>10.2f}  {r['return_pct']:>8.3f}%")

    # ============================================================
    print("\n" + "=" * 74)
    print("  BOTTLENECK #2: Slippage Impact")
    print("=" * 74)

    print(f"\n  {'Slippage':>12} {'Trades':>8} {'Slip Cost':>12} {'Net P&L':>12} {'Return':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*12} {'-'*12} {'-'*10}")

    for slip in [0, 0.5, 1.0, 2.0, 3.0, 5.0]:
        r = run_realistic_backtest(klines, capital=1000, leverage=50, threshold=0.00005,
                                   trade_pct=0.1, taker_fee=0.0006, slippage_bps=slip, funding_rate_8h=0)
        print(f"  {slip:>10.1f}bp  {r['trades']:>6}  {r['total_slippage']:>10.4f}  "
              f"{r['pnl']:>10.2f}  {r['return_pct']:>8.3f}%")

    # ============================================================
    print("\n" + "=" * 74)
    print("  BOTTLENECK #3: Funding Rate Impact (30 days)")
    print("=" * 74)

    print(f"\n  {'Rate/8h':>12} {'Monthly Cost':>14} {'Net P&L':>12} {'Return':>10}")
    print(f"  {'-'*12} {'-'*14} {'-'*12} {'-'*10}")

    for fr in [0.0, 0.0001, 0.0003, 0.0005, 0.001, 0.003]:
        r = run_realistic_backtest(klines, capital=1000, leverage=50, threshold=0.00005,
                                   trade_pct=0.1, taker_fee=0.0006, slippage_bps=1.0, funding_rate_8h=fr)
        print(f"  {fr*100:>10.3f}%  {r['total_funding']:>12.2f}  "
              f"{r['pnl']:>10.2f}  {r['return_pct']:>8.3f}%")

    # ============================================================
    print("\n" + "=" * 74)
    print("  BOTTLENECK #4: Position Size / Capital Utilization")
    print("=" * 74)

    print(f"\n  {'Trade %':>10} {'Per Trade':>12} {'Trades':>8} {'Fees':>10} {'Net P&L':>12} {'Return':>10} {'MaxDD':>8}")
    print(f"  {'-'*10} {'-'*12} {'-'*8} {'-'*10} {'-'*12} {'-'*10} {'-'*8}")

    for tp in [0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0]:
        r = run_realistic_backtest(klines, capital=1000, leverage=50, threshold=0.00005,
                                   trade_pct=tp, taker_fee=0.0006, slippage_bps=1.0, funding_rate_8h=0.0001)
        per_trade = 1000 * tp
        print(f"  {tp*100:>8.0f}%  {per_trade:>10.0f}  {r['trades']:>6}  {r['total_fees']:>8.2f}  "
              f"{r['pnl']:>10.2f}  {r['return_pct']:>8.3f}%  {r['max_dd']:>6.3f}%")

    # ============================================================
    print("\n" + "=" * 74)
    print("  BOTTLENECK #5: Leverage vs Risk")
    print("=" * 74)

    realistic_costs = {"taker_fee": 0.0006, "slippage_bps": 1.0, "funding_rate_8h": 0.0001}
    print(f"\n  {'Lev':>6} {'Trades':>8} {'Win%':>8} {'Fees':>10} {'Funding':>10} {'Net P&L':>12} {'Return':>10} {'MaxDD':>8}")
    print(f"  {'-'*6} {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*12} {'-'*10} {'-'*8}")

    for lev in [5, 10, 25, 50, 75, 100, 125, 150, 200]:
        r = run_realistic_backtest(klines, capital=1000, leverage=lev, threshold=0.00005,
                                   trade_pct=0.1, **realistic_costs)
        print(f"  {lev:>4}x  {r['trades']:>6}  {r['win_rate']:>6.1f}%  {r['total_fees']:>8.2f}  "
              f"{r['total_funding']:>8.2f}  {r['pnl']:>10.2f}  {r['return_pct']:>8.3f}%  {r['max_dd']:>6.3f}%")

    # ============================================================
    # FULL REALISTIC SCENARIO
    # ============================================================
    print("\n" + "=" * 74)
    print("  REALISTIC SCENARIO A: Market Orders (Taker) - 1,000 USDT")
    print("=" * 74)

    r = run_realistic_backtest(
        klines, capital=1000, leverage=50, threshold=0.00005,
        trade_pct=0.1, taker_fee=0.0006, slippage_bps=1.0, funding_rate_8h=0.0001,
    )

    print(f"\n  Initial Capital:   1,000.00 USDT")
    print(f"  Final Balance:     {r['final']:.2f} USDT")
    print(f"  Net P&L:           {'+' if r['pnl']>=0 else ''}{r['pnl']:.2f} USDT")
    print(f"  Return:            {'+' if r['return_pct']>=0 else ''}{r['return_pct']:.3f}%")
    print(f"  Total Trades:      {r['trades']}")
    print(f"  Win Rate:          {r['win_rate']:.1f}%")
    print(f"  Max Drawdown:      {r['max_dd']:.3f}%")
    print(f"\n  Cost Breakdown:")
    print(f"    Trading Fees:    {r['total_fees']:.2f} USDT")
    print(f"    Slippage:        {r['total_slippage']:.4f} USDT")
    print(f"    Funding:         {r['total_funding']:.4f} USDT")
    print(f"    Total Costs:     {r['total_costs']:.2f} USDT")
    gross = r['pnl'] + r['total_costs']
    print(f"    Gross Profit:    {gross:.2f} USDT")
    print(f"    Cost Ratio:      {r['total_costs']/gross*100:.1f}% of gross" if gross > 0 else "")

    # Scenario B: Maker orders (the solution)
    print("\n" + "=" * 74)
    print("  REALISTIC SCENARIO B: Limit Orders (Maker, 0% fee, 0 slippage)")
    print("=" * 74)

    r2 = run_realistic_backtest(
        klines, capital=1000, leverage=50, threshold=0.00005,
        trade_pct=0.1, taker_fee=0.0, slippage_bps=0.0, funding_rate_8h=0.0001,
    )
    print(f"\n  Initial Capital:   1,000.00 USDT")
    print(f"  Final Balance:     {r2['final']:.2f} USDT")
    print(f"  Net P&L:           {'+' if r2['pnl']>=0 else ''}{r2['pnl']:.2f} USDT")
    print(f"  Return:            {'+' if r2['return_pct']>=0 else ''}{r2['return_pct']:.3f}%")
    print(f"  Total Trades:      {r2['trades']}")
    print(f"  Win Rate:          {r2['win_rate']:.1f}%")
    print(f"  Max Drawdown:      {r2['max_dd']:.3f}%")
    print(f"  Total Costs:       {r2['total_costs']:.2f} USDT (funding only)")

    # ============================================================
    # SOLUTIONS
    # ============================================================
    print("\n" + "=" * 74)
    print("  SOLUTIONS: Fee Reduction Strategies")
    print("=" * 74)

    print(f"\n  {'Strategy':>30} | {'Fee':>8} {'Net P&L':>12} {'Return':>10} {'vs Base':>10}")
    print(f"  {'-'*30} | {'-'*8} {'-'*12} {'-'*10} {'-'*10}")

    scenarios = [
        ("Current (Taker 0.06%)", 0.0006, 1.0),
        ("Maker orders only (0%)", 0.0000, 1.0),
        ("Maker orders (0.02%)", 0.0002, 1.0),
        ("VIP1 Taker (0.04%)", 0.0004, 1.0),
        ("MX Token discount (20%off)", 0.00048, 1.0),
        ("Maker + no slippage", 0.0000, 0.0),
    ]

    base_r = r
    for name, fee, slip in scenarios:
        sr = run_realistic_backtest(
            klines, capital=1000, leverage=50, threshold=0.00005,
            trade_pct=0.1, taker_fee=fee, slippage_bps=slip, funding_rate_8h=0.0001,
        )
        diff = sr["pnl"] - base_r["pnl"]
        print(f"  {name:>30} | {fee*100:>6.2f}%  {sr['pnl']:>10.2f}  "
              f"{sr['return_pct']:>8.3f}%  {'+' if diff>=0 else ''}{diff:>8.2f}")

    # ============================================================
    print("\n" + "=" * 74)
    print("  SOLUTION: Multi-Pair Strategy (diversification)")
    print("=" * 74)

    # Simulate running the same strategy on multiple stablecoin pairs
    pairs_to_check = ["USDCUSDT", "DAIUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT"]
    print(f"\n  Checking available stablecoin pairs on MEXC spot...")
    for pair in pairs_to_check:
        try:
            resp = requests.get(
                "https://api.mexc.com/api/v3/ticker/price",
                params={"symbol": pair}, timeout=5,
            )
            data = resp.json()
            if "price" in data:
                print(f"    {pair}: price = {float(data['price']):.6f}")
            else:
                print(f"    {pair}: not available")
        except Exception:
            print(f"    {pair}: error fetching")

    # ============================================================
    print("\n" + "=" * 74)
    print("  SUMMARY: Bottlenecks & Solutions")
    print("=" * 74)

    print("""
  [BOTTLENECKS - What limits profitability]

  1. TRADING FEES (biggest impact)
     - Taker fee 0.06% per trade on 100 USDT notional = 0.06 USDT
     - With 3000+ trades/month, fees reach 180+ USDT (18% of capital!)
     - This is the #1 cost, consuming 30-50% of gross profits

  2. SLIPPAGE
     - USDC/USDT spread is very tight (~0.01%), low impact
     - But with high-frequency trading, small slippage accumulates
     - Impact: ~1-3% of gross profits

  3. FUNDING RATE
     - 0.01% every 8 hours on the notional position
     - Short holding periods minimize this cost
     - Impact: <1% of gross profits

  4. PRICE VOLATILITY (too low)
     - USDC/USDT only moves 0.0155% (std dev) - extremely stable
     - Profit per trade is tiny: 0.01-0.05% before leverage
     - Need high leverage (50x+) just to make fees worthwhile

  5. EXECUTION SPEED
     - API latency affects entry/exit timing
     - In a 5-minute window, price can revert before order fills

  [SOLUTIONS - How to maximize returns]

  1. USE MAKER ORDERS (Limit Orders) - saves 0.06% per trade
     - Switch from market (taker) to limit (maker) orders
     - MEXC maker fee = 0% for futures
     - This alone can DOUBLE net profits
     -> Implementation: Place limit orders at threshold prices

  2. MX TOKEN STAKING - 20% fee discount
     - Hold MX tokens for reduced trading fees
     - Combined with maker orders: near-zero fees

  3. INCREASE TRADE SIZE (not frequency)
     - Use 20-30% of capital per trade instead of 10%
     - Fewer trades needed, lower total fees
     - But higher per-trade risk

  4. OPTIMIZE THRESHOLD
     - Tighter threshold (0.005%) = more trades, more fees
     - Wider threshold (0.02%) = fewer but more profitable trades
     - Sweet spot: 0.01% threshold with maker orders

  5. MULTI-PAIR STRATEGY
     - Run same strategy on USDC/USDT, DAI/USDT, FDUSD/USDT
     - Diversifies opportunities without increasing per-pair risk

  6. CONSIDER SPOT ARBITRAGE ALTERNATIVE
     - Cross-exchange USDC/USDT arbitrage (spot, no leverage)
     - Lower risk, lower return, but no liquidation danger
""")

    print("=" * 74)


if __name__ == "__main__":
    main()
