"""A Shopify Admin GraphQL client that reads, with two narrow exceptions.

`query()` refuses any document containing a mutation before anything is sent,
so reads can't change a store even with a token that allows it. Exactly two
mutations exist, each sent from its own method only:

- `bulkOperationRunQuery` (starts a read-only bulk export), from `start_bulk_export`;
- `customerEmailMarketingConsentUpdate` (email marketing consent and nothing
  else, batched as aliases), from `update_email_consents`, used only by
  `shopify consent-sync`.

Shopify rate-limits GraphQL by query cost and reports it in the
response (`THROTTLED`), so throttled queries wait for the bucket to refill.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from migtool.config import Secret
from migtool.http import ApiError, HttpClient

API_VERSION = "2026-07"
MUTATION = re.compile(r"\bmutation\b")


BULK_START = """mutation($q: String!) {
  bulkOperationRunQuery(query: $q) { bulkOperation { id status } userErrors { field message } }
}"""
BULK_STATUS = """query($id: ID!) { node(id: $id) { ... on BulkOperation {
  id status type errorCode objectCount url query createdAt } } }"""
BULK_LIST = """query($n: Int!) { bulkOperations(first: $n, sortKey: CREATED_AT, reverse: true) {
  nodes { id status type errorCode objectCount url query createdAt } } }"""


CONSENT_READ = """query($ids: [ID!]!) { nodes(ids: $ids) { ... on Customer {
  id email emailMarketingConsent { marketingState marketingOptInLevel consentUpdatedAt } } } }"""
# Shopify refuses NOT_SUBSCRIBED as an input (consent-sync pilot, 2026-10-02).
CONSENT_STATES = ("SUBSCRIBED", "UNSUBSCRIBED")
# Consent updates per request, as GraphQL aliases. A consent mutation costs about
# 10 points; Shopify Plus allows 20,000 points with 1,000 restored per second.
CONSENT_UPDATE_BATCH = 50
_CONSENT_FIELDS = """customerEmailMarketingConsentUpdate(input: $i%d) {
    customer { id emailMarketingConsent { marketingState marketingOptInLevel consentUpdatedAt } }
    userErrors { field message code } }"""


def customer_gid(customer_id: str) -> str:
    """A numeric customer ID (as the customers export writes it) as a GraphQL ID."""
    return customer_id if customer_id.startswith("gid://") else f"gid://shopify/Customer/{customer_id}"


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
        # Queries are safe to retry. The two mutations are sent with retries off
        # (a lost response is looked up instead), so nothing to warn about.
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

    def email_consents(self, customer_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Current email marketing consent for up to 250 customers (one query):
        customer ID as given → `emailMarketingConsent` (with `email`), or None
        when there's no such customer."""
        gids = [customer_gid(c) for c in customer_ids]
        nodes = self.query(CONSENT_READ, {"ids": gids})["nodes"]
        return {c: node for c, node in zip(customer_ids, nodes)}

    def update_email_consents(
        self, updates: list[tuple[str, str, str | None]],
    ) -> dict[str, dict[str, Any] | None]:
        """Set email marketing consent for up to CONSENT_UPDATE_BATCH customers in
        one request: (customer ID, state, consentUpdatedAt) each, sent as
        aliases of `customerEmailMarketingConsentUpdate`. Sends nothing else.

        Returns customer ID → Shopify's result for it (`customer`, `userErrors`),
        or None when the response has no result for it. A throttled request (not
        executed) waits and is sent again; a lost response is never resent here
        (`ApiError`): the caller reads the customers back."""
        if not updates or len(updates) > CONSENT_UPDATE_BATCH:
            raise ValueError(f"Send 1 to {CONSENT_UPDATE_BATCH} consent updates per request, not {len(updates)}.")
        variables: dict[str, Any] = {}
        for n, (customer_id, state, consent_updated_at) in enumerate(updates):
            if state not in CONSENT_STATES:
                raise ValueError(f"Email consent can only be set to {' or '.join(CONSENT_STATES)}, not {state!r}.")
            consent: dict[str, Any] = {"marketingState": state}
            if state == "SUBSCRIBED":
                consent["marketingOptInLevel"] = "SINGLE_OPT_IN"
            if consent_updated_at:
                consent["consentUpdatedAt"] = consent_updated_at
            variables[f"i{n}"] = {"customerId": customer_gid(customer_id), "emailMarketingConsent": consent}
        document = ("mutation(" + ", ".join(f"$i{n}: CustomerEmailMarketingConsentUpdateInput!"
                                             for n in range(len(updates))) + ") {\n"
                    + "\n".join(f"  c{n}: " + _CONSENT_FIELDS % n for n in range(len(updates))) + "\n}")
        for _ in range(self._max_throttled + 1):
            body = self._http.post("/graphql.json", json={"query": document, "variables": variables},
                                   retry_writes=False).json()
            errors = body.get("errors") or []
            if errors and all((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                self._sleep(_throttle_wait(body))
                continue
            data = body.get("data")
            if errors and not data:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors))
            return {cid: (data or {}).get(f"c{n}") for n, (cid, _, _) in enumerate(updates)}
        raise ShopifyError("Still throttled after retrying; try again later.")

    def recent_bulk_queries(self, limit: int = 10) -> list[dict[str, Any]]:
        """The store's latest bulk query operations, newest first."""
        data = self.query(BULK_LIST, {"n": limit})["bulkOperations"]["nodes"]
        return [op for op in data if op.get("type") == "QUERY"]

    def bulk_operation(self, op_id: str) -> dict[str, Any]:
        return self.query(BULK_STATUS, {"id": op_id})["node"]

    def start_bulk_export(self, inner_query: str) -> str:
        """Start a read-only bulk export of `inner_query` and return its operation ID.
        `bulkOperationRunQuery` is the only mutation this client sends, and only
        from here, with a query that must itself contain no mutation. It's not
        retried after a response is lost; the new operation is looked up instead,
        so a retry can't start a second export. Only an operation that wasn't
        there before the start counts, so an older export of the same query is
        never taken for this one."""
        if MUTATION.search(inner_query):
            raise ValueError("A bulk export query can't contain a mutation.")
        before = {op["id"] for op in self.recent_bulk_queries(10)}
        try:
            body = self._http.post("/graphql.json", json={"query": BULK_START, "variables": {"q": inner_query}},
                                   retry_writes=False).json()
        except ApiError as exc:
            if exc.status is not None and exc.status < 500:
                raise
            recent = [op for op in self.recent_bulk_queries(10) if op["id"] not in before
                      and _same_query(op.get("query"), inner_query)
                      and op["status"] in ("CREATED", "RUNNING", "COMPLETED")]
            if len(recent) == 1:
                return recent[0]["id"]
            raise ShopifyError(f"Starting the bulk export failed ({exc.detail}) and it can't be told whether it "
                               "started; check with `recent_bulk_queries` before trying again.") from exc
        if body.get("errors"):
            raise ShopifyError("; ".join(e.get("message", str(e)) for e in body["errors"]))
        started = body["data"]["bulkOperationRunQuery"]
        if started["userErrors"]:
            raise ShopifyError("; ".join(e["message"] for e in started["userErrors"]))
        return started["bulkOperation"]["id"]

    def wait_bulk(self, op_id: str, *, poll_seconds: float = 10, timeout: float = 4 * 3600,
                  progress: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
        """Poll a bulk operation until it finishes; returns it (with `url` when complete)."""
        waited = 0.0
        while True:
            op = self.bulk_operation(op_id)
            progress(f"  bulk export {op['status'].lower()}: {int(op.get('objectCount') or 0):,} objects")
            if op["status"] == "COMPLETED":
                return op
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


def _same_query(a: str | None, b: str) -> bool:
    return " ".join((a or "").split()) == " ".join(b.split())
