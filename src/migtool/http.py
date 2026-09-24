"""Shared HTTP client: retries, Retry-After and rate limiting.

429 and 5xx responses, and connection failures, are retried with increasing
waits up to `max_attempts` attempts in total, honouring `Retry-After` when the
service sends it. Credentials go in request headers only and never appear in
errors.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class ApiError(Exception):
    """A request that failed for good: a non-retryable error, or retries used up."""

    def __init__(self, method: str, url: str, status: int | None, detail: str) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.detail = detail
        where = f"{method} {url}"
        super().__init__(f"{where} -> {status}: {detail}" if status else f"{where}: {detail}")


@dataclass(frozen=True)
class Limit:
    """At most `requests` requests (or cost points) per `seconds`."""

    requests: float
    seconds: float


class _Bucket:
    def __init__(self, limit: Limit, now: float) -> None:
        self.capacity = limit.requests
        self.rate = limit.requests / limit.seconds
        self.tokens = limit.requests
        self.updated = now

    def refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_for(self, cost: float) -> float:
        return max(0.0, (cost - self.tokens) / self.rate)


class RateLimiter:
    """Token buckets that must all allow a request before it is sent.

    Klaviyo publishes a burst (per second) and a steady (per minute) limit per
    endpoint, which are two `Limit`s here. STOQ's 360 points per minute is one
    `Limit` with each write costing 2.
    """

    def __init__(
        self, limits: Iterable[Limit], *, clock: Clock = time.monotonic, sleep: Sleep = time.sleep
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        now = clock()
        self._buckets = [_Bucket(limit, now) for limit in limits]

    def acquire(self, cost: float = 1) -> None:
        while True:
            now = self._clock()
            for b in self._buckets:
                b.refill(now)
            wait = max((b.wait_for(cost) for b in self._buckets), default=0.0)
            # Tolerance: rounding can leave a wait too small to ever elapse.
            if wait <= 1e-9:
                for b in self._buckets:
                    b.tokens -= cost
                return
            self._sleep(wait)


def retry_after_seconds(response: httpx.Response, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (seconds or HTTP date), if present."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


class HttpClient:
    """An httpx client with retries and optional rate limiting."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        limiter: RateLimiter | None = None,
        max_attempts: int = 6,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        timeout: float = 60.0,
        sleep: Sleep = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )
        self._limiter = limiter
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep

    def backoff(self, attempt: int) -> float:
        """Wait before retry number `attempt` (1-based) when the service gives no Retry-After."""
        return min(self._backoff_max, self._backoff_base * 2 ** (attempt - 1))

    def request(
        self, method: str, url: str, *, cost: float = 1, limiter: RateLimiter | None = None, **kwargs
    ) -> httpx.Response:
        """Send a request, retrying 429, 5xx and connection failures.

        `limiter` overrides the client's default limiter for this request, for
        services whose limits differ per endpoint. Raises ApiError for any other
        4xx, or once all attempts are used.
        """
        limiter = limiter or self._limiter
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            if limiter is not None:
                limiter.acquire(cost)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise ApiError(method, url, None, f"{type(exc).__name__}: {exc}") from exc
                self._sleep(self.backoff(attempt))
                continue
            if _is_retryable(response.status_code) and not last:
                wait = retry_after_seconds(response)
                self._sleep(self.backoff(attempt) if wait is None else wait)
                continue
            if response.is_error:
                raise ApiError(method, str(response.url), response.status_code, response.text[:500])
            return response
        raise AssertionError("unreachable")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
