"""Klaviyo US → Shopify US email consent sync: planning and validation.

Local and read-only: both work from export files (`klaviyo profiles export`,
`shopify customers-export`). The rules are `docs/CONSENT_SYNC.md`, decisions
D1–D15 and section 4.1:

- Klaviyo is the source of truth for email marketing consent (D1, D11).
- Klaviyo subscribed → Shopify `SUBSCRIBED`; unsubscribed or suppressed →
  `UNSUBSCRIBED` (D3, D10); never subscribed + Shopify `NOT_SUBSCRIBED` is a
  match.
- Shopify can't be set to `NOT_SUBSCRIBED`, so never subscribed + Shopify
  `UNSUBSCRIBED` is left as is (D12), and never subscribed + Shopify
  `SUBSCRIBED` is kept and Klaviyo is subscribed with Shopify's date (D14).
- The date written is the original one (D5): `ca_consent_timestamp` /
  `ca_suppression_timestamp` on migrated CA profiles, Klaviyo's own otherwise.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from migtool.output import parse_iso

PLAN_COLUMNS = ["shopify_customer_id", "email", "klaviyo_profile_id", "klaviyo_state", "shopify_state",
                "target_state", "target_date", "shopify_date", "shopify_newer"]
D14_COLUMNS = ["Email Marketing Consent", "Email Marketing Consent Timestamp", "email", "shopify_customer_id"]
EXCLUDED_COLUMNS = ["email", "shopify_customer_id", "klaviyo_profile_id", "klaviyo_state", "shopify_state", "reason"]
MISMATCH_COLUMNS = ["email", "shopify_customer_id", "klaviyo_profile_id", "problem"]
KLAVIYO_CHANGE_COLUMNS = ["email", "klaviyo_profile_id", "field", "before", "after"]

TARGET = {"subscribed": "SUBSCRIBED", "unsubscribed": "UNSUBSCRIBED", "suppressed": "UNSUBSCRIBED"}
UNWRITABLE = {"INVALID", "REDACTED"}


def iso_z(value: str) -> str:
    """A timestamp as Shopify takes it: UTC, whole seconds, `Z`."""
    return parse_iso(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def same_second(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return not a and not b
    return parse_iso(a).replace(microsecond=0) == parse_iso(b).replace(microsecond=0)


def read_csv(path: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(1 << 30)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def shopify_by_email(path: Path) -> dict[str, dict[str, str]]:
    """Shopify customers export rows by lowercased email (customers without an
    email can't be matched). Raises if an email is on more than one customer."""
    out: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        email = (row.get("email") or "").strip().lower()
        if not email:
            continue
        if email in out:
            raise ValueError(f"{email} is on more than one Shopify customer; resolve that before planning.")
        out[email] = row
    return out


@dataclass(frozen=True)
class KlaviyoConsent:
    profile_id: str
    email: str
    state: str  # subscribed, unsubscribed, suppressed, never
    date: str  # the original consent / suppression date (D5), or ""
    consent_timestamp: str
    method: str


def _property(row: dict[str, str], name: str) -> str:
    """A `properties.<name>` column, whatever its `#type` suffix."""
    for column, value in row.items():
        if column and column.split("#")[0] == f"properties.{name}":
            return value or ""
    return ""


def klaviyo_consent(row: dict[str, str]) -> KlaviyoConsent:
    """One `profiles export` row's email consent, as the sync sees it. A
    suppression other than UNSUBSCRIBE makes it `suppressed`; an UNSUBSCRIBE
    suppression on a profile that isn't subscribed makes it `unsubscribed`."""
    suppressions = json.loads(row.get("suppressions") or "[]")
    strong = [s for s in suppressions if s.get("reason") != "UNSUBSCRIBE"]
    migrated = _property(row, "migrated_from") == "ca"
    if strong:
        state = "suppressed"
        date = (migrated and _property(row, "ca_suppression_timestamp")) or strong[0].get("timestamp") \
            or row.get("suppression_timestamp") or ""
    else:
        state = {"SUBSCRIBED": "subscribed", "UNSUBSCRIBED": "unsubscribed"}.get(row.get("consent") or "", "never")
        if state == "never" and any(s.get("reason") == "UNSUBSCRIBE" for s in suppressions):
            state = "unsubscribed"
        date = (migrated and _property(row, "ca_consent_timestamp")) or row.get("consent_timestamp") or ""
    return KlaviyoConsent(row.get("id") or "", (row.get("email") or "").strip().lower(), state, date,
                          row.get("consent_timestamp") or "", row.get("method") or "")


def action(klaviyo_state: str, shopify_state: str) -> str:
    """`match`, `write`, `d14`, `d12` or `excluded` for one matched customer."""
    if shopify_state in UNWRITABLE:
        return "excluded"
    if klaviyo_state == "never":
        return {"NOT_SUBSCRIBED": "match", "UNSUBSCRIBED": "d12", "SUBSCRIBED": "d14"}.get(shopify_state, "write")
    return "match" if shopify_state == TARGET[klaviyo_state] else "write"


@dataclass
class Plan:
    writes: list[dict[str, Any]] = field(default_factory=list)
    d14: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)  # (klaviyo state, shopify state, action) → n
    profiles: int = 0
    matched: int = 0
    no_date: int = 0
    clamped: int = 0
    shopify_newer: int = 0


def plan(klaviyo_path: Path, shopify_path: Path, *, now: datetime, only: set[str] | None = None) -> Plan:
    """Work out every write. `only` limits the plan to these emails (trials)."""
    shop = shopify_by_email(shopify_path)
    result = Plan()
    for row in read_csv(klaviyo_path):
        result.profiles += 1
        k = klaviyo_consent(row)
        if not k.email or (only is not None and k.email not in only):
            continue
        s = shop.get(k.email)
        if not s:
            continue
        result.matched += 1
        sh_state = s.get("email_marketing") or "NOT_SUBSCRIBED"
        act = action(k.state, sh_state)
        result.counts[(k.state, sh_state, act)] += 1
        base = {"email": k.email, "shopify_customer_id": s["customer_id"], "klaviyo_profile_id": k.profile_id}
        if act == "write":
            date = ""
            if k.date:
                at = parse_iso(k.date)
                if at > now:  # Shopify refuses future dates (pilot P8a)
                    at, result.clamped = now, result.clamped + 1
                date = at.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                result.no_date += 1
            sh_date = s.get("email_consent_updated") or ""
            newer = bool(sh_date and date and parse_iso(sh_date) > parse_iso(date))
            result.shopify_newer += newer
            result.writes.append({**base, "klaviyo_state": k.state, "shopify_state": sh_state,
                                  "target_state": TARGET[k.state], "target_date": date,
                                  "shopify_date": sh_date, "shopify_newer": newer})
        elif act == "d14":
            result.d14.append({"Email Marketing Consent": "Subscribe",
                               "Email Marketing Consent Timestamp": s.get("email_consent_updated") or "",
                               "email": k.email, "shopify_customer_id": s["customer_id"]})
        elif act in ("d12", "excluded"):
            reason = ("D12: Klaviyo never subscribed, Shopify unsubscribed; NOT_SUBSCRIBED can't be set"
                      if act == "d12" else f"Shopify email state {sh_state} can't be written")
            result.excluded.append({**base, "klaviyo_state": k.state, "shopify_state": sh_state, "reason": reason})
    return result


# --- Validation ------------------------------------------------------------------


@dataclass
class Validation:
    counts: Counter = field(default_factory=Counter)
    mismatches: list[dict[str, str]] = field(default_factory=list)
    klaviyo_changes: list[dict[str, str]] = field(default_factory=list)


def written_targets(results_paths: list[Path]) -> dict[str, tuple[str, str]]:
    """Shopify customer ID → (state, date) for every customer a sync run wrote."""
    out: dict[str, tuple[str, str]] = {}
    for path in results_paths:
        for row in read_csv(path):
            if row.get("outcome") == "written":
                out[row["shopify_customer_id"]] = (row["target_state"], row["target_date"])
    return out


def validate(
    klaviyo_path: Path, shopify_path: Path, *, written: dict[str, tuple[str, str]] | None = None,
    d14_emails: set[str] | None = None, klaviyo_before: Path | None = None,
) -> Validation:
    """Check post-run exports against the rules. A customer is fine when its
    action is `match` or `d12`; `write` or `d14` means it still differs. For
    customers the run wrote, Shopify's date must equal the date written (dates
    are only set with a state change, pilot P8b). For D14 customers, Klaviyo's
    date must equal Shopify's. With `klaviyo_before`, every change to Klaviyo
    consent state, date or method is listed for review (D14 ones are expected)."""
    shop = shopify_by_email(shopify_path)
    written = written or {}
    d14_emails = d14_emails or set()
    v = Validation()
    after: dict[str, KlaviyoConsent] = {}
    for row in read_csv(klaviyo_path):
        k = klaviyo_consent(row)
        after[k.profile_id] = k
        s = shop.get(k.email) if k.email else None
        if not s:
            continue
        sh_state = s.get("email_marketing") or "NOT_SUBSCRIBED"
        act = action(k.state, sh_state)
        v.counts["checked"] += 1
        base = {"email": k.email, "shopify_customer_id": s["customer_id"], "klaviyo_profile_id": k.profile_id}
        problems = []
        if act in ("write", "d14"):
            problems.append(f"Klaviyo {k.state}, Shopify {sh_state}")
        elif act == "excluded":
            v.counts["excluded"] += 1
        elif act == "d12":
            v.counts["d12"] += 1
        target = written.get(s["customer_id"])
        if target and not problems:
            if sh_state != target[0]:
                problems.append(f"Shopify {sh_state}, the run wrote {target[0]}")
            elif target[1] and not same_second(s.get("email_consent_updated"), target[1]):
                problems.append(f"Shopify date {s.get('email_consent_updated')}, the run wrote {target[1]}")
        if k.email in d14_emails and not problems and not same_second(k.consent_timestamp, s.get("email_consent_updated")):
            problems.append(f"D14: Klaviyo date {k.consent_timestamp}, Shopify date {s.get('email_consent_updated')}")
        if problems:
            v.counts["mismatched"] += 1
            v.mismatches.append({**base, "problem": "; ".join(problems)})
        else:
            v.counts["ok"] += 1
    if klaviyo_before:
        for row in read_csv(klaviyo_before):
            b = klaviyo_consent(row)
            a = after.get(b.profile_id)
            if not a:
                continue
            for name, before, now in (("state", b.state, a.state), ("consent_timestamp", b.consent_timestamp,
                                      a.consent_timestamp), ("method", b.method, a.method)):
                if before != now and not (name == "consent_timestamp" and same_second(before, now)):
                    v.counts["klaviyo_changed_d14" if a.email in d14_emails else "klaviyo_changed"] += 1
                    v.klaviyo_changes.append({"email": a.email, "klaviyo_profile_id": a.profile_id, "field": name,
                                              "before": before, "after": now})
    return v
