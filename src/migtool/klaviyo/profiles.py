"""Profile export: one CSV row per profile, in the layout `profiles import` reads.

Standard fields are columns, `location.*` and `predictive_analytics.*` are
flattened, custom properties become `properties.<key>` (nested values as JSON
text), and email marketing consent is one column per detail.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from migtool.klaviyo.client import KlaviyoClient
from migtool.output import ResumableExport, iso, iso_or_none, parse_iso

STANDARD = [
    "id", "email", "phone_number", "external_id", "anonymous_id",
    "first_name", "last_name", "organization", "title", "locale", "image",
    "created", "updated", "last_event_date",
]
LOCATION = [
    "address1", "address2", "city", "region", "zip", "country",
    "latitude", "longitude", "timezone", "ip",
]
CONSENT = [
    "consent", "consent_timestamp", "consent_last_updated", "method", "method_detail",
    "custom_method_detail", "double_optin", "can_receive_email_marketing",
    "suppression_reason", "suppression_timestamp", "suppressions", "list_suppressions",
]
TIMESTAMPS = ["created", "updated", "last_event_date"]
COLUMNS = STANDARD + [f"location.{k}" for k in LOCATION] + CONSENT

# Get Profiles: 75/s and 700/min, or 10/s and 150/min with predictive analytics.
TIER = "L"
PREDICTIVE_TIER = "M"


def email_marketing(attrs: dict[str, Any]) -> dict[str, Any]:
    subs = attrs.get("subscriptions") or {}
    return ((subs.get("email") or {}).get("marketing")) or {}


def latest_suppression(marketing: dict[str, Any]) -> dict[str, Any]:
    items = marketing.get("suppression") or []
    return max(items, key=lambda s: s.get("timestamp") or "", default={})


def flatten(profile: dict[str, Any]) -> dict[str, Any]:
    """One profile resource as a CSV row."""
    attrs = profile["attributes"]
    row: dict[str, Any] = {"id": profile["id"]}
    for key in STANDARD[1:]:
        row[key] = attrs.get(key)
    for key in TIMESTAMPS:
        row[key] = iso_or_none(row[key])
    for key, value in (attrs.get("location") or {}).items():
        row[f"location.{key}"] = value
    m = email_marketing(attrs)
    latest = latest_suppression(m)
    row.update(
        consent=m.get("consent"),
        consent_timestamp=iso_or_none(m.get("consent_timestamp")),
        consent_last_updated=iso_or_none(m.get("last_updated")),
        method=m.get("method"),
        method_detail=m.get("method_detail"),
        custom_method_detail=m.get("custom_method_detail"),
        double_optin=m.get("double_optin"),
        can_receive_email_marketing=m.get("can_receive_email_marketing"),
        suppression_reason=latest.get("reason"),
        suppression_timestamp=iso_or_none(latest.get("timestamp")),
        suppressions=m.get("suppression") or None,
        list_suppressions=m.get("list_suppressions") or None,
    )
    for key, value in (attrs.get("properties") or {}).items():
        row[f"properties.{key}"] = value
    for key, value in (attrs.get("predictive_analytics") or {}).items():
        row[f"predictive_analytics.{key}"] = value
    return row


def profile_params(
    *, since: datetime | None, predictive: bool, in_group: bool
) -> dict[str, str]:
    extra = "subscriptions,predictive_analytics" if predictive else "subscriptions"
    params = {"additional-fields[profile]": extra, "page[size]": "100"}
    # List and segment member endpoints can't filter on `updated`; see `export`.
    if since and not in_group:
        params["filter"] = f"greater-than(updated,{iso(since)})"
    return params


def export(
    client: KlaviyoClient,
    exp: ResumableExport,
    *,
    segment_id: str | None = None,
    since: datetime | None = None,
    predictive: bool = False,
    progress: Callable[[int], None] = lambda rows: None,
) -> int:
    """Write every matching profile to `exp`, checkpointing after each page.
    Returns the number of profiles skipped by `--since` on a segment export."""
    path = f"/segments/{segment_id}/profiles/" if segment_id else "/profiles/"
    params = profile_params(since=since, predictive=predictive, in_group=bool(segment_id))
    tier = PREDICTIVE_TIER if predictive else TIER
    skipped = 0
    for page, nxt in client.pages(path, tier=tier, start=exp.cursor, params=params):
        for profile in page["data"]:
            if segment_id and since and not _updated_after(profile, since):
                skipped += 1
                continue
            exp.write(flatten(profile))
        exp.checkpoint(nxt)
        progress(exp.rows)
        if nxt is None:
            break
    return skipped


def _updated_after(profile: dict[str, Any], since: datetime) -> bool:
    updated = profile["attributes"].get("updated")
    return bool(updated) and parse_iso(updated) > since


def resolve_segment(client: KlaviyoClient, value: str) -> tuple[str, str]:
    """A segment ID or exact name → (id, name). Ambiguous or unknown names raise."""
    matches = []
    for seg in iter_segments(client):
        if seg["id"] == value:
            return seg["id"], seg["attributes"]["name"]
        if seg["attributes"]["name"].casefold() == value.casefold():
            matches.append(seg)
    if len(matches) == 1:
        return matches[0]["id"], matches[0]["attributes"]["name"]
    if not matches:
        raise LookupError(f"No segment with ID or name '{value}'.")
    ids = ", ".join(f"{s['id']} ({s['attributes']['name']})" for s in matches)
    raise LookupError(f"'{value}' matches {len(matches)} segments: {ids}. Use the ID.")


def iter_segments(client: KlaviyoClient, fields: str = "name") -> Iterator[dict[str, Any]]:
    for page in client.paginate("/segments/", tier="L", params={"fields[segment]": fields}):
        yield from page["data"]
