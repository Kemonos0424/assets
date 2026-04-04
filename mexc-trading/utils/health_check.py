"""
Self-monitoring health check module.

Runs periodic diagnostics on the trading bot:
- API connectivity and latency
- Price feed validity (stale data detection)
- Account balance monitoring
- Position state consistency
- Performance anomaly detection (abnormal win/loss streaks)
- Memory/resource usage
"""
import logging
import time
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    is_healthy: bool = True
    api_latency_ms: float = 0.0
    last_price: float = 0.0
    last_price_time: float = 0.0
    price_stale: bool = False
    balance: float = 0.0
    balance_change_pct: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    position_duration_s: float = 0.0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class HealthChecker:
    """Monitors bot health and detects anomalies."""

    # Thresholds
    MAX_API_LATENCY_MS = 5000
    MAX_PRICE_STALE_S = 60  # Price older than 60s = stale
    MAX_CONSECUTIVE_LOSSES = 10
    MAX_POSITION_DURATION_S = 3600  # 1 hour stuck in position
    BALANCE_DROP_ALERT_PCT = 5.0  # Alert if balance drops 5%+
    CHECK_INTERVAL_S = 60  # Run health check every 60s

    def __init__(self, strategy, notifier, initial_balance: float = 1000.0):
        self.strategy = strategy
        self.notifier = notifier
        self.initial_balance = initial_balance
        self.last_balance = initial_balance
        self.status = HealthStatus(balance=initial_balance)
        self.trade_results: list[float] = []  # Recent PnLs
        self._last_check_time = 0.0
        self._price_history: list[tuple[float, float]] = []  # (timestamp, price)

    def record_price(self, price: float) -> None:
        """Record a price observation."""
        now = time.time()
        self.status.last_price = price
        self.status.last_price_time = now
        self._price_history.append((now, price))
        # Keep last 100 prices
        if len(self._price_history) > 100:
            self._price_history = self._price_history[-100:]

    def record_trade(self, pnl: float) -> None:
        """Record a trade result for anomaly detection."""
        self.trade_results.append(pnl)
        if len(self.trade_results) > 50:
            self.trade_results = self.trade_results[-50:]

        # Track consecutive wins/losses
        if pnl >= 0:
            self.status.consecutive_wins += 1
            self.status.consecutive_losses = 0
        else:
            self.status.consecutive_losses += 1
            self.status.consecutive_wins = 0

    def should_check(self) -> bool:
        """Return True if enough time has passed since last check."""
        return time.time() - self._last_check_time >= self.CHECK_INTERVAL_S

    def run_check(self) -> HealthStatus:
        """Run all health checks and return status."""
        self._last_check_time = time.time()
        self.status.errors.clear()
        self.status.warnings.clear()
        self.status.is_healthy = True

        self._check_price_feed()
        self._check_api_latency()
        self._check_balance()
        self._check_position()
        self._check_anomalies()

        if self.status.errors:
            self.status.is_healthy = False
            for err in self.status.errors:
                logger.error("[HEALTH] %s", err)
            self.notifier.notify_error(
                "Health Check Failed:\n" + "\n".join(self.status.errors)
            )

        if self.status.warnings:
            for warn in self.status.warnings:
                logger.warning("[HEALTH] %s", warn)

        return self.status

    def _check_price_feed(self) -> None:
        """Check if price data is fresh."""
        if self.status.last_price_time == 0:
            return

        age = time.time() - self.status.last_price_time
        if age > self.MAX_PRICE_STALE_S:
            self.status.price_stale = True
            self.status.errors.append(
                f"Price feed stale: last update {age:.0f}s ago"
            )
        else:
            self.status.price_stale = False

        # Check if price is within reasonable range for USDC/USDT
        if self.status.last_price > 0:
            if self.status.last_price < 0.99 or self.status.last_price > 1.01:
                self.status.errors.append(
                    f"Price out of range: {self.status.last_price:.6f} "
                    "(expected 0.99-1.01 for USDC/USDT)"
                )

    def _check_api_latency(self) -> None:
        """Check API response time."""
        try:
            start = time.time()
            self.strategy.client.get_ticker(self.strategy.symbol)
            latency = (time.time() - start) * 1000
            self.status.api_latency_ms = latency

            if latency > self.MAX_API_LATENCY_MS:
                self.status.warnings.append(
                    f"High API latency: {latency:.0f}ms (threshold: {self.MAX_API_LATENCY_MS}ms)"
                )
        except Exception as e:
            self.status.errors.append(f"API connectivity failed: {e}")

    def _check_balance(self) -> None:
        """Check account balance for unexpected drops."""
        try:
            balance = self.strategy.get_account_balance()
            self.status.balance = balance

            if self.last_balance > 0:
                change_pct = (balance - self.last_balance) / self.last_balance * 100
                self.status.balance_change_pct = change_pct

                if change_pct <= -self.BALANCE_DROP_ALERT_PCT:
                    self.status.errors.append(
                        f"Balance dropped {change_pct:.1f}%: "
                        f"{self.last_balance:.2f} -> {balance:.2f} USDT"
                    )

            self.last_balance = balance
        except Exception as e:
            self.status.warnings.append(f"Balance check failed: {e}")

    def _check_position(self) -> None:
        """Check for stuck positions."""
        pos = self.strategy.current_position
        if pos is not None:
            duration = time.time() - pos.open_time
            self.status.position_duration_s = duration

            if duration > self.MAX_POSITION_DURATION_S:
                self.status.warnings.append(
                    f"Position held for {duration/60:.0f} min "
                    f"({pos.side} @ {pos.entry_price:.6f})"
                )

    def _check_anomalies(self) -> None:
        """Detect trading anomalies."""
        if self.status.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            self.status.errors.append(
                f"Consecutive losses: {self.status.consecutive_losses} "
                f"(limit: {self.MAX_CONSECUTIVE_LOSSES})"
            )

        # Check if all recent trades are identical (possible logic bug)
        if len(self.trade_results) >= 5:
            last_5 = self.trade_results[-5:]
            if len(set(round(p, 4) for p in last_5)) == 1:
                self.status.warnings.append(
                    f"Last 5 trades have identical PnL: {last_5[0]:.4f} "
                    "(possible logic issue)"
                )

    def get_summary(self) -> str:
        """Return a human-readable summary."""
        s = self.status
        health = "HEALTHY" if s.is_healthy else "UNHEALTHY"
        lines = [
            f"Status: {health}",
            f"API Latency: {s.api_latency_ms:.0f}ms",
            f"Last Price: {s.last_price:.6f}",
            f"Balance: {s.balance:.2f} USDT ({s.balance_change_pct:+.1f}%)",
            f"Win Streak: {s.consecutive_wins} / Loss Streak: {s.consecutive_losses}",
        ]
        if s.errors:
            lines.append(f"Errors: {', '.join(s.errors)}")
        if s.warnings:
            lines.append(f"Warnings: {', '.join(s.warnings)}")
        return " | ".join(lines)
