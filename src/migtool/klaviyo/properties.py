"""Set one custom property on existing profiles, identified by email.

Used for holds like `catchup_hold=true`: a flow profile filter `<key> equals
false` then skips the listed profiles, while profiles without the property
pass. Only existing profiles are updated; emails with no profile are skipped,
never created (a bare profile would carry nothing but the hold).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from migtool.klaviyo.dedupe import _same, fetch_profiles, parse_bool
from migtool.klaviyo.writes import finite_number

KINDS = ("bool", "number", "text")
CHECK_COLUMNS = ["email", "problem"]


def parse_value(text: str, kind: str) -> Any:
    """The `--value` as the type `--type` says. Raises ValueError."""
    if kind == "bool":
        return parse_bool(text)
    if kind == "number":
        return finite_number(text)
    if kind == "text":
        return text
    raise ValueError(f"'{kind}' isn't one of {', '.join(KINDS)}")


def check_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise ValueError("the property name is empty")
    if key.startswith("$"):
        raise ValueError(f"'{key}' is a Klaviyo-internal property name")
    return key


def plan(
    rows: list[dict[str, str]], existing: Callable[[list[str]], set[str]],
) -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    """The rows whose email has a profile, and the rest as (email, reason).
    `rows` are already usable (lowercased, unique emails)."""
    found = existing([r["email"] for r in rows]) if rows else set()
    keep, skipped = [], []
    for row in rows:
        if row["email"] in found:
            keep.append(row)
        else:
            skipped.append((row["email"], "no existing profile in the destination (update only)"))
    return keep, skipped


def payloads(rows: list[dict[str, str]], key: str, value: Any) -> list[dict[str, Any]]:
    """Email and the one property: nothing else on the profile is touched."""
    return [{"email": r["email"], "properties": {key: value}} for r in rows]


def check(
    client, emails: list[str], key: str, value: Any, progress: Callable[[str], None] = lambda _: None,
) -> tuple[dict[str, int], list[dict[str, str]]]:
    """Read every profile back and compare the property, type for type.
    Returns (counts, mismatch rows). Read-only."""
    progress(f"reading {len(emails):,} profiles")
    profiles = fetch_profiles(client, emails, [])
    counts = {"checked": 0, "ok": 0, "no profile": 0, "mismatched": 0}
    mismatches: list[dict[str, str]] = []
    for email in emails:
        counts["checked"] += 1
        attrs = profiles.get(email)
        if attrs is None:
            counts["no profile"] += 1
            mismatches.append({"email": email, "problem": "no profile"})
            continue
        stored = (attrs.get("properties") or {}).get(key)
        if _same(value, stored):
            counts["ok"] += 1
        else:
            counts["mismatched"] += 1
            mismatches.append({"email": email, "problem": f"{key} is {stored!r}, expected {value!r}"})
    return counts, mismatches
