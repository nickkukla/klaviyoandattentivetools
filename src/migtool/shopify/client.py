"""A read-only Shopify Admin GraphQL client.

Every request is a GraphQL query; a document containing a mutation is refused
before anything is sent, so the tool can't change a store even with a token
that allows it. The one exception is starting a bulk export
(`bulkOperationRunQuery`), which only reads, sent from `bulk_export` alone. Shopify rate-limits GraphQL by query cost and reports it in the
response (`THROTTLED`), so throttled queries wait for the bucket to refill.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from migtool.config import Secret
from migtool.http import HttpClient

API_VERSION = "2026-07"
MUTATION = re.compile(r"\bmutation\b")


BULK_START = """mutation($q: String!) {
  bulkOperationRunQuery(query: $q) { bulkOperation { id status } userErrors { field message } }
}"""
BULK_STATUS = """query($id: ID!) { node(id: $id) { ... on BulkOperation {
  id status errorCode objectCount url } } }"""


class ShopifyError(Exception):
    """GraphQL errors in an otherwise successful response."""


class ShopifyClient:
    def __init__(
        self, shop: str, token: Secret, *, transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep, max_throttled: int = 10,
    ) -> None:
        self.shop = shop
        self._sleep = sleep
        self._max_throttled = max_throttled
        # Queries only, so a retried request can't be applied twice: nothing to warn about.
        self._http = HttpClient(
            f"https://{shop}/admin/api/{API_VERSION}",
            headers={"X-Shopify-Access-Token": token.reveal(), "Content-Type": "application/json"},
            transport=transport, sleep=sleep, warn=lambda _: None,
        )

    def query(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if MUTATION.search(document):
            raise ValueError("This client is read-only: GraphQL mutations aren't allowed.")
        for _ in range(self._max_throttled + 1):
            body = self._http.post("/graphql.json", json={"query": document, "variables": variables or {}}).json()
            errors = body.get("errors") or []
            if errors and all((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                self._sleep(_throttle_wait(body))
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors))
            return body["data"]
        raise ShopifyError("Still throttled after retrying; try again later.")

    def bulk_export(self, inner_query: str, *, poll_seconds: float = 10, timeout: float = 4 * 3600,
                    progress: Callable[[str], None] = lambda _: None) -> str | None:
        """Run a read-only bulk export of `inner_query` and return the URL of its
        JSONL result (None when there's nothing). Starting one takes the
        `bulkOperationRunQuery` mutation: the only mutation this client sends,
        and only from here, with a query that must itself contain no mutation."""
        if MUTATION.search(inner_query):
            raise ValueError("A bulk export query can't contain a mutation.")
        running = self.query("query { currentBulkOperation { id status } }")["currentBulkOperation"]
        if running and running["status"] in ("CREATED", "RUNNING"):
            raise ShopifyError(f"Another bulk operation ({running['id']}) is still running; try again when it finishes.")
        body = self._http.post("/graphql.json", json={"query": BULK_START, "variables": {"q": inner_query}}).json()
        if body.get("errors"):
            raise ShopifyError("; ".join(e.get("message", str(e)) for e in body["errors"]))
        started = body["data"]["bulkOperationRunQuery"]
        if started["userErrors"]:
            raise ShopifyError("; ".join(e["message"] for e in started["userErrors"]))
        op_id = started["bulkOperation"]["id"]
        waited = 0.0
        while True:
            op = self.query(BULK_STATUS, {"id": op_id})["node"]
            progress(f"  bulk export {op['status'].lower()}: {int(op.get('objectCount') or 0):,} objects")
            if op["status"] == "COMPLETED":
                return op.get("url")
            if op["status"] in ("FAILED", "CANCELED", "EXPIRED"):
                raise ShopifyError(f"Bulk export {op['status'].lower()}: {op.get('errorCode')}")
            if waited >= timeout:
                raise ShopifyError(f"Bulk export still {op['status'].lower()} after {int(timeout)}s ({op_id}).")
            self._sleep(poll_seconds)
            waited += poll_seconds

    def shop_info(self) -> dict[str, Any]:
        data = self.query("""query { shop { name myshopifyDomain }
            currentAppInstallation { accessScopes { handle } } }""")
        return {"name": data["shop"]["name"], "domain": data["shop"]["myshopifyDomain"],
                "scopes": sorted(s["handle"] for s in data["currentAppInstallation"]["accessScopes"])}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ShopifyClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _throttle_wait(body: dict[str, Any]) -> float:
    """Seconds until the cost bucket holds what the query needs (at least 1)."""
    cost = (body.get("extensions") or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    needed = cost.get("requestedQueryCost") or 0
    available = status.get("currentlyAvailable") or 0
    rate = status.get("restoreRate") or 50
    return max(1.0, (needed - available) / rate)
