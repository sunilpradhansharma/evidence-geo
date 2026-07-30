"""Per-target token-bucket rate limiter (FR-207)."""
import asyncio
import time


class TokenBucket:
    """Simple async token bucket for requests-per-minute limiting."""

    def __init__(self, rpm: int):
        self.rpm = max(rpm, 1)
        self.capacity = float(self.rpm)
        self.tokens = float(self.rpm)
        self.refill_rate = self.rpm / 60.0  # tokens per second
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.updated
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.refill_rate
                await asyncio.sleep(wait)


class RateLimiterRegistry:
    """Holds one bucket per target name."""

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}

    def get(self, target_name: str, rpm: int) -> TokenBucket:
        if target_name not in self._buckets:
            self._buckets[target_name] = TokenBucket(rpm)
        return self._buckets[target_name]
