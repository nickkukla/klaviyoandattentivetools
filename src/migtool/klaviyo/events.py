"""Re-send an order's Placed Order data as a custom event, to trigger a flow
(say, a copy of Order Confirmation) for orders whose original flow email was
blocked. The new event carries an exact copy of the original event's
properties, so the flow's template renders as it would have."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from migtool.klaviyo.client import KlaviyoClient

SOURCE_METRIC = ("Placed Order", "Shopify")


@dataclass
class Resend:
    row: dict[str, str]
    customer: str
    recipient: str
    order: str
    event: dict[str, Any] | None = None  # the source Placed Order event
    problem: str = ""


@dataclass
class Plan:
    items: list[Resend] = field(default_factory=list)

    @property
    def ready(self) -> list[Resend]:
        return [r for r in self.items if r.event and not r.problem]


def source_metric_id(client: KlaviyoClient, name: str = SOURCE_METRIC[0], integration: str = SOURCE_METRIC[1]) -> str:
    for page in client.paginate("/metrics/", params={"fields[metric]": "name,integration"}):
        for m in page["data"]:
            if m["attributes"]["name"] == name and (m["attributes"].get("integration") or {}).get("name") == integration:
                return m["id"]
    raise LookupError(f"No '{name}' metric from {integration} in this account.")


def order_id(event: dict[str, Any]) -> str:
    p = event["attributes"]["event_properties"]
    return str((p.get("$extra") or {}).get("id") or p.get("$event_id") or "")


def order_name(event: dict[str, Any]) -> str:
    return str((event["attributes"]["event_properties"].get("$extra") or {}).get("name") or "")


def _matches(event: dict[str, Any], want_id: str, want_name: str) -> bool:
    """Every identifier the row gives must match this order (ID and name are
    compared separately, so a stale name can't pick another order)."""
    return (not want_id or order_id(event) == want_id) and (not want_name or order_name(event) == want_name)


def plan(client: KlaviyoClient, rows: list[dict[str, str]], *, metric_id: str, send_to: str | None) -> Plan:
    """Find each row's source Placed Order event on the customer's profile,
    matched by `order_id` or `order_name`."""
    out = Plan()
    for row in rows:
        customer = (row.get("email") or "").strip().lower()
        order = (row.get("order_id") or row.get("order_name") or "").strip()
        item = Resend(row, customer, (send_to or customer).strip().lower(), order)
        out.items.append(item)
        if not customer or not order:
            item.problem = "needs email and order_id or order_name"
            continue
        profiles = client.get("/profiles/", tier="L", params={"filter": f"equals(email,{json.dumps(customer)})"})["data"]
        if not profiles:
            item.problem = "no Klaviyo profile"
            continue
        want_id, want_name = (row.get("order_id") or "").strip(), (row.get("order_name") or "").strip()
        params = {"filter": f'equals(profile_id,"{profiles[0]["id"]}"),equals(metric_id,"{metric_id}")'}
        found = [ev for page in client.paginate("/events/", tier="L", params=params)
                 for ev in page["data"] if _matches(ev, want_id, want_name)]
        if len({ev["id"] for ev in found}) > 1:
            item.problem = f"{len(found)} Placed Order events match {order}; resolve it by hand"
        elif not found:
            item.problem = (f"no Placed Order event with id {want_id or '(any)'} and name {want_name or '(any)'} "
                            "on that profile")
        else:
            item.event = found[0]
    return out


def event_body(item: Resend, *, metric: str, time: str) -> dict[str, Any]:
    source = item.event["attributes"]
    props = copy.deepcopy(source["event_properties"])
    props["resent_from_event_id"] = item.event["id"]
    props["resent_from_time"] = source.get("datetime")
    # One resend per order and recipient: Klaviyo drops a repeat with the same unique_id.
    unique = f"resend-{order_id(item.event) or item.event['id']}"
    if item.recipient != item.customer:
        unique += f"-to-{item.recipient}"
    props.pop("$event_id", None)
    attrs: dict[str, Any] = {
        "properties": props, "time": time, "unique_id": unique,
        "metric": {"data": {"type": "metric", "attributes": {"name": metric}}},
        "profile": {"data": {"type": "profile", "attributes": {"email": item.recipient}}},
    }
    if source.get("event_properties", {}).get("$value") is not None:
        attrs["value"] = source["event_properties"]["$value"]
    return {"data": {"type": "event", "attributes": attrs}}


def send(client: KlaviyoClient, body: dict[str, Any]) -> None:
    client.post("/events/", body, tier="L")
