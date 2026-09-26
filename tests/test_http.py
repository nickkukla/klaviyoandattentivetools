import httpx
import pytest
import respx

from migtool.http import ApiError, HttpClient, Limit, RateLimiter, retry_after_seconds

BASE = "https://api.example.test"


def make_client(clock, **kwargs):
    return HttpClient(BASE, headers={"Authorization": "Bearer sk_secret"}, sleep=clock.sleep, **kwargs)


@respx.mock
def test_429_honours_retry_after(clock):
    route = respx.get(f"{BASE}/x").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    assert make_client(clock).get("/x").status_code == 200
    assert route.call_count == 2
    assert clock.sleeps == [7.0]


@respx.mock
def test_5xx_backs_off_and_gives_up_after_6_attempts(clock):
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(ApiError) as err:
        make_client(clock).get("/x")
    assert route.call_count == 6
    assert clock.sleeps == [1, 2, 4, 8, 16]
    assert err.value.status == 503


@respx.mock
def test_backoff_is_capped(clock):
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(500))
    with pytest.raises(ApiError):
        make_client(clock, backoff_base=10, backoff_max=30).get("/x")
    assert clock.sleeps == [10, 20, 30, 30, 30]


@respx.mock
def test_other_4xx_is_not_retried(clock):
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(ApiError, match="400"):
        make_client(clock).get("/x")
    assert route.call_count == 1
    assert clock.sleeps == []


@respx.mock
def test_connection_errors_are_retried(clock):
    respx.get(f"{BASE}/x").mock(side_effect=[httpx.ConnectError("reset"), httpx.Response(200)])
    assert make_client(clock).get("/x").status_code == 200
    assert clock.sleeps == [1]


@respx.mock
def test_errors_never_contain_the_key(clock):
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(ApiError) as err:
        make_client(clock).get("/x")
    assert "sk_secret" not in str(err.value)


def test_retry_after_http_date():
    from datetime import UTC, datetime

    resp = httpx.Response(429, headers={"Retry-After": "Thu, 24 Sep 2026 15:30:10 GMT"})
    now = datetime(2026, 9, 24, 15, 30, 0, tzinfo=UTC)
    assert retry_after_seconds(resp, now) == 10


def test_limiter_allows_burst_then_paces(clock):
    limiter = RateLimiter([Limit(3, 1)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    assert clock.sleeps == []
    limiter.acquire()
    assert clock.sleeps == [pytest.approx(1 / 3)]


def test_limiter_counts_cost_points(clock):
    # 360 points per minute, 2 points per request: 180 requests per minute.
    limiter = RateLimiter([Limit(360, 60)], clock=clock, sleep=clock.sleep)
    for _ in range(180 + 180):
        limiter.acquire(cost=2)
    assert clock.now == pytest.approx(60)


def test_limiter_uses_the_tighter_of_burst_and_steady(clock):
    # Klaviyo-style: 10 per second burst, 150 per minute steady.
    limiter = RateLimiter([Limit(10, 1), Limit(150, 60)], clock=clock, sleep=clock.sleep)
    for _ in range(150 + 150):
        limiter.acquire()
    assert clock.now == pytest.approx(60)


@respx.mock
def test_client_uses_limiter_on_every_attempt(clock):
    respx.get(f"{BASE}/x").mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    limiter = RateLimiter([Limit(1, 10)], clock=clock, sleep=clock.sleep)
    make_client(clock, limiter=limiter).get("/x")
    # 1s backoff, then the limiter waits the rest of its 10s window.
    assert clock.sleeps == [1, pytest.approx(9)]


@respx.mock
def test_write_retried_after_lost_response_warns(clock):
    warnings = []
    route = respx.post(f"{BASE}/jobs").mock(side_effect=[httpx.ReadTimeout("lost"), httpx.Response(202, json={})])
    c = HttpClient(BASE, sleep=clock.sleep, warn=warnings.append)
    assert c.post("/jobs").status_code == 202
    assert route.call_count == 2
    assert len(warnings) == 1 and "may be sent twice" in warnings[0] and "ReadTimeout" in warnings[0]


@respx.mock
def test_no_warning_when_connection_never_opened_or_for_reads(clock):
    warnings = []
    respx.post(f"{BASE}/jobs").mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(202, json={})])
    respx.get(f"{BASE}/x").mock(side_effect=[httpx.ReadTimeout("lost"), httpx.Response(200, json={})])
    c = HttpClient(BASE, sleep=clock.sleep, warn=warnings.append)
    c.post("/jobs"); c.get("/x")
    assert warnings == []


@respx.mock
def test_write_retried_after_server_error_warns(clock):
    warnings = []
    respx.post(f"{BASE}/jobs").mock(side_effect=[httpx.Response(502), httpx.Response(202, json={})])
    HttpClient(BASE, sleep=clock.sleep, warn=warnings.append).post("/jobs")
    assert len(warnings) == 1 and "got 502" in warnings[0]
