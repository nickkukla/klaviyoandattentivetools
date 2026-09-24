"""Segment labels: which kinds of events each segment's rules depend on.

Labels come from each `profile-metric` condition's metric and that metric's
source integration. Rules on profile properties, consent, list or segment
membership, or location add no label.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from migtool.klaviyo.client import KlaviyoClient

COLUMNS = [
    "id", "name", "created", "updated", "member_count",
    "engagement", "site_activity", "third_party", "events",
]

# Decided by event name, whatever integration sends it (in our accounts some
# of these arrive through the API rather than Shopify).
SITE_ACTIVITY = {"Viewed Product", "Active on Site", "Added to Cart", "Checkout Started"}
SUBSCRIPTION_EVENTS = {
    "Subscribed to List", "Unsubscribed from List",
    "Subscribed to Email Marketing", "Unsubscribed from Email Marketing",
    "Subscribed to Back in Stock",
}
ORDER_EVENTS = {"Placed Order", "Ordered Product"}


def label(name: str, integration: str | None) -> str | None:
    """`engagement`, `site_activity`, `third_party` or None for one metric."""
    key = (integration or "").casefold()
    if name in SITE_ACTIVITY:
        return "site_activity"
    if name in SUBSCRIPTION_EVENTS:
        return None
    if key == "klaviyo":
        return "engagement" if "Email" in name else None
    if name in ORDER_EVENTS or (key == "shopify" and "Order" in name):
        return "engagement"
    if key == "shopify":
        return "site_activity"
    return "third_party"


def metric_ids(definition: dict[str, Any] | None) -> Iterator[str]:
    for group in (definition or {}).get("condition_groups") or []:
        for cond in group.get("conditions") or []:
            if cond.get("type") == "profile-metric" and cond.get("metric_id"):
                yield cond["metric_id"]


def metrics(client: KlaviyoClient) -> dict[str, tuple[str, str | None]]:
    """Metric ID → (name, integration key)."""
    out: dict[str, tuple[str, str | None]] = {}
    for page in client.paginate("/metrics/", tier="M", params={"fields[metric]": "name,integration"}):
        for m in page["data"]:
            integ = m["attributes"].get("integration") or {}
            out[m["id"]] = (m["attributes"]["name"], integ.get("key") or integ.get("name"))
    return out


def labels(
    definition: dict[str, Any] | None, known: dict[str, tuple[str, str | None]]
) -> dict[str, Any]:
    """The label columns for one segment definition."""
    names: set[str] = set()
    found: set[str] = set()
    for mid in metric_ids(definition):
        if mid not in known:  # deleted metric: listed, but no label
            names.add(f"unknown metric {mid}")
            continue
        name, integration = known[mid]
        names.add(name)
        if (lab := label(name, integration)) is not None:
            found.add(lab)
    return {
        "engagement": "engagement" in found,
        "site_activity": "site_activity" in found,
        "third_party": "third_party" in found,
        "events": "; ".join(sorted(names)),
    }
