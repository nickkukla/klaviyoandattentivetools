"""Back in Stock export: "Subscribed to Back in Stock" events → STOQ's import template.

Klaviyo has no list of Back in Stock subscriptions; they exist only as events,
read here with the linked profile. Products are identified by the event's SKU,
because variant and product IDs differ between the CA and US stores.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from migtool.klaviyo.client import KlaviyoClient
from migtool.klaviyo.profiles import email_marketing
from migtool.output import iso, iso_or_none, parse_iso

METRIC_NAME = "Subscribed to Back in Stock"

# STOQ Import Template columns, in order. Header names must match for STOQ to
# map them automatically.
STOQ_COLUMNS = [
    "SKU", "Email", "Phone", "Name", "Market", "Quantity",
    "GDPR confirmed", "Accepts marketing", "Language", "Date",
]
REFERENCE_COLUMNS = [
    "Email", "SKU", "ca_variant_id", "ca_product_id", "product_name", "variant_name",
    "signed_up_at", "ca_event_id",
]
EXCLUDED_COLUMNS = ["Email", "SKU", "signed_up_at", "reason", "ca_event_id"]


def metric_id(client: KlaviyoClient, name: str = METRIC_NAME) -> str:
    """The Back in Stock metric's ID. Klaviyo can't filter metrics by name."""
    for page in client.paginate("/metrics/", tier="M", params={"fields[metric]": "name"}):
        for m in page["data"]:
            if m["attributes"]["name"] == name:
                return m["id"]
    raise LookupError(f"No metric named '{name}' in this account.")


def stoq_date(timestamp: str) -> str:
    """`dd/mm/yyyy` (UTC), one of the two date formats STOQ accepts."""
    return parse_iso(timestamp).strftime("%d/%m/%Y")


def language(profile: dict[str, Any]) -> str:
    """A `Language` property if present, otherwise the locale's language code
    (`en-CA` → `en`)."""
    value = (profile.get("properties") or {}).get("Language") or profile.get("locale") or ""
    return str(value).replace("_", "-").split("-")[0].lower()


def accepts_marketing(profile: dict[str, Any]) -> bool:
    """Subscribed to email marketing and not suppressed, right now."""
    m = email_marketing(profile)
    return m.get("consent") == "SUBSCRIBED" and not m.get("suppression")


def consent(client: KlaviyoClient, profile_ids: list[str], cache: dict[str, Any]) -> None:
    """Fill `cache` with each profile's `subscriptions`. Profiles included with
    events come back with `subscriptions: null`, so they're read separately."""
    todo = sorted({i for i in profile_ids if i not in cache})
    for i in range(0, len(todo), 100):
        ids = ",".join(json.dumps(x) for x in todo[i:i + 100])
        params = {
            "filter": f"any(id,[{ids}])",
            "additional-fields[profile]": "subscriptions",
            "fields[profile]": "subscriptions",
            "page[size]": "100",
        }
        for page in client.paginate("/profiles/", tier="L", params=params):
            for p in page["data"]:
                cache[p["id"]] = p["attributes"].get("subscriptions")
    for pid in todo:
        cache.setdefault(pid, None)


def events(
    client: KlaviyoClient, metric: str, since: datetime | None
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """(event, profile attributes) pairs, newest first. Profile attributes
    include `subscriptions`, read from the profiles endpoint."""
    flt = f'equals(metric_id,"{metric}")'
    if since:
        flt = f"and({flt},greater-than(datetime,{iso(since)}))"
    params = {
        "filter": flt,
        "sort": "-datetime",
        "include": "profile",
        "fields[event]": "datetime,event_properties",
        "fields[profile]": "email,first_name,last_name,locale,properties",
        "page[size]": "200",
    }
    subscriptions: dict[str, Any] = {}
    for page in client.paginate("/events/", tier="L", params=params):
        profiles = {p["id"]: p["attributes"] for p in page.get("included") or [] if p["type"] == "profile"}
        consent(client, list(profiles), subscriptions)
        for event in page["data"]:
            link = (event.get("relationships", {}).get("profile", {}).get("data")) or {}
            pid = link.get("id")
            attrs = profiles.get(pid)
            yield event, ({**attrs, "subscriptions": subscriptions.get(pid)} if attrs else {})


def build(
    pairs: Iterator[tuple[dict[str, Any], dict[str, Any]]],
    *,
    write: Callable[[dict], None],
    reference: Callable[[dict], None],
    exclude: Callable[[dict], None],
) -> dict[str, int]:
    """Turn newest-first events into STOQ rows: the latest signup per email and
    SKU is kept, everything dropped goes to `exclude` with a reason."""
    kept: dict[tuple[str, str], str] = {}
    counts = {"events": 0, "exported": 0, "excluded": 0}
    for event, profile in pairs:
        counts["events"] += 1
        props = event["attributes"].get("event_properties") or {}
        email = (profile.get("email") or "").strip().lower()
        sku = str(props.get("SKU") or "").strip()
        when = iso_or_none(event["attributes"].get("datetime")) or ""
        base = {"Email": email, "SKU": sku, "signed_up_at": when, "ca_event_id": event["id"]}
        if not email:
            reason = "profile has no email"
        elif not sku:
            reason = "event has no SKU"
        elif (email, sku) in kept:
            reason = f"older signup for the same email and SKU (kept {kept[(email, sku)]})"
        else:
            reason = None
        if reason:
            exclude({**base, "reason": reason})
            counts["excluded"] += 1
            continue
        kept[(email, sku)] = when
        name = " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p)
        write({
            "SKU": sku, "Email": email, "Phone": "", "Name": name, "Market": "", "Quantity": "",
            "GDPR confirmed": "", "Accepts marketing": "true" if accepts_marketing(profile) else "false",
            "Language": language(profile), "Date": stoq_date(when) if when else "",
        })
        reference({
            **base, "ca_variant_id": props.get("VariantId"), "ca_product_id": props.get("ProductID"),
            "product_name": props.get("ProductName"), "variant_name": props.get("VariantName"),
        })
        counts["exported"] += 1
    return counts
