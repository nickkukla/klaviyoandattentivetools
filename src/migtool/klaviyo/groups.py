"""Lists and segments: metadata plus one row per member."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from migtool.klaviyo.client import KlaviyoClient
from migtool.output import iso, iso_or_none

MEMBER_COLUMNS = ["{kind}_id", "{kind}_name", "profile_id", "email", "joined_group_at"]


def member_columns(kind: str) -> list[str]:
    return [c.format(kind=kind) for c in MEMBER_COLUMNS]


def members(
    client: KlaviyoClient, kind: str, group: dict[str, Any], *, since: datetime | None = None
) -> Iterator[dict[str, Any]]:
    """Member rows for one list or segment. `since` keeps members who joined after it."""
    params = {"fields[profile]": "email,joined_group_at", "page[size]": "100"}
    if since:
        params["filter"] = f"greater-than(joined_group_at,{iso(since)})"
    name = group["attributes"]["name"]
    for page in client.paginate(f"/{kind}s/{group['id']}/profiles/", tier="L", params=params):
        for p in page["data"]:
            yield {
                f"{kind}_id": group["id"],
                f"{kind}_name": name,
                "profile_id": p["id"],
                "email": p["attributes"].get("email"),
                "joined_group_at": iso_or_none(p["attributes"].get("joined_group_at")),
            }


def all_groups(client: KlaviyoClient, kind: str, fields: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in client.paginate(f"/{kind}s/", tier="L", params={f"fields[{kind}]": fields}):
        out.extend(page["data"])
    return out


def lists_named(client: KlaviyoClient, name: str) -> list[str]:
    """IDs of the lists called exactly `name`."""
    ids: list[str] = []
    for page in client.paginate("/lists/", tier="L", params={"fields[list]": "name", "filter": f'equals(name,"{name}")'}):
        ids.extend(g["id"] for g in page["data"] if g["attributes"]["name"] == name)
    return ids
