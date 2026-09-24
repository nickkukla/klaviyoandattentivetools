"""Suppression export: one row per email suppression, with reason and date."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from migtool.klaviyo.client import KlaviyoClient
from migtool.klaviyo.profiles import TIER, email_marketing
from migtool.output import ResumableExport, iso, iso_or_none, parse_iso

COLUMNS = ["email", "profile_id", "reason", "timestamp"]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def export(
    client: KlaviyoClient,
    exp: ResumableExport,
    *,
    since: datetime | None = None,
    progress: Callable[[int], None] = lambda rows: None,
) -> None:
    """Write every suppression (or every one after `since`) to `exp`.

    Klaviyo filters on the suppression timestamp, so a profile suppressed
    before `since` isn't read at all. A profile can hold several suppressions;
    each is a row, and with `since` only the newer ones are kept.
    """
    after = since or EPOCH
    params = {
        "filter": f"greater-than(subscriptions.email.marketing.suppression.timestamp,{iso(after)})",
        "additional-fields[profile]": "subscriptions",
        "fields[profile]": "email,subscriptions",
        "page[size]": "100",
    }
    for page, nxt in client.pages("/profiles/", tier=TIER, start=exp.cursor, params=params):
        for profile in page["data"]:
            attrs = profile["attributes"]
            for item in email_marketing(attrs).get("suppression") or []:
                ts = item.get("timestamp")
                if since and (not ts or parse_iso(ts) <= since):
                    continue
                exp.write(
                    {"email": attrs.get("email"), "profile_id": profile["id"],
                     "reason": item.get("reason"), "timestamp": iso_or_none(ts)}
                )
        exp.checkpoint(nxt)
        progress(exp.rows)
        if nxt is None:
            break
