import pytest


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def klaviyo_account():
    """Mock Klaviyo's /accounts/ lookup (every command checks the key's account
    first). Call it inside a respx-mocked test with the expected account ID."""
    import httpx
    import respx

    def mock(account_id: str, name: str = "Test account") -> None:
        respx.get("https://a.klaviyo.com/api/accounts/").mock(return_value=httpx.Response(200, json={"data": [
            {"id": account_id, "attributes": {"contact_information": {"organization_name": name}}}]}))
    return mock
