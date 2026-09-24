"""Klaviyo API client: auth, pinned revision and per-endpoint rate limits."""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from migtool.config import Secret
from migtool.http import HttpClient, Limit, RateLimiter

BASE_URL = "https://a.klaviyo.com/api"
REVISION = "2026-07-15"

# Klaviyo's published tiers: burst per second, steady per minute.
TIERS: dict[str, tuple[Limit, Limit]] = {
    "XS": (Limit(1, 1), Limit(15, 60)),
    "S": (Limit(3, 1), Limit(60, 60)),
    "M": (Limit(10, 1), Limit(150, 60)),
    "L": (Limit(75, 1), Limit(700, 60)),
    "XL": (Limit(350, 1), Limit(3500, 60)),
}


class KlaviyoClient:
    def __init__(self, api_key: Secret, *, transport: httpx.BaseTransport | None = None) -> None:
        self._http = HttpClient(
            BASE_URL,
            headers={
                "Authorization": f"Klaviyo-API-Key {api_key.reveal()}",
                "revision": REVISION,
                "accept": "application/vnd.api+json",
                "content-type": "application/vnd.api+json",
            },
            transport=transport,
        )
        self._limiters: dict[str, RateLimiter] = {}

    def _limiter(self, tier: str) -> RateLimiter:
        if tier not in self._limiters:
            self._limiters[tier] = RateLimiter(TIERS[tier])
        return self._limiters[tier]

    def get(self, path: str, *, tier: str = "M", **kwargs) -> dict:
        return self._http.get(path, limiter=self._limiter(tier), **kwargs).json()

    def paginate(self, path: str, *, tier: str = "M", **kwargs) -> Iterator[dict]:
        """Yield each response page, following `links.next` cursors."""
        page = self.get(path, tier=tier, **kwargs)
        yield page
        while nxt := (page.get("links") or {}).get("next"):
            page = self.get(nxt, tier=tier)
            yield page

    def account(self) -> dict:
        """The account the key belongs to: `id` and `name`."""
        data = self.get("/accounts/", tier="XS")["data"][0]
        return {
            "id": data["id"],
            "name": data["attributes"]["contact_information"]["organization_name"],
        }

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KlaviyoClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
