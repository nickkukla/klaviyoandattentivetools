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
  Where Shopify's consent date is newer than or equal to it (or there's no
  date), the write is dated at the sync time instead (D16): Shopify silently
  ignores a change dated before its current consent date.
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
                "target_state", "target_date", "date_rule", "klaviyo_date", "shopify_date", "shopify_newer"]
# D16: Shopify silently ignores a consent change dated before the customer's
# current consent date (trial, 2026-10-02). Such writes are dated at the sync time.
DATE_ORIGINAL = "original"
DATE_SYNC_TIME = "sync time"
D14_COLUMNS = ["Email Marketing Consent", "Email Marketing Consent Timestamp", "email", "shopify_customer_id"]
EXCLUDED_COLUMNS = ["email", "shopify_customer_id", "klaviyo_profile_id", "klaviyo_state", "shopify_state", "reason"]
MISMATCH_COLUMNS = ["email", "shopify_customer_id", "klaviyo_profile_id", "problem"]
KLAVIYO_CHANGE_COLUMNS = ["email", "klaviyo_profile_id", "field", "before", "after"]

TARGET = {"subscribed": "SUBSCRIBED", "unsubscribed": "UNSUBSCRIBED", "suppressed": "UNSUBSCRIBED"}
UNWRITABLE = {"INVALID", "REDACTED"}
KLAVIYO_CONSENTS = {"SUBSCRIBED", "UNSUBSCRIBED", "NEVER_SUBSCRIBED"}
SHOPIFY_STATES = {"SUBSCRIBED", "NOT_SUBSCRIBED", "UNSUBSCRIBED", "PENDING", "INVALID", "REDACTED"}
# Columns each export must have; a file without them is refused rather than
# read as "never subscribed" (missing data is not a consent decision).
KLAVIYO_COLUMNS = {"id", "email", "consent", "consent_timestamp", "suppressions"}
SHOPIFY_COLUMNS = {"customer_id", "email", "email_marketing", "email_consent_updated"}


def iso_z(value: str) -> str:
    """A timestamp as Shopify takes it: UTC, whole seconds, `Z`."""
    return parse_iso(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def same_second(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return not a and not b
    return parse_iso(a).replace(microsecond=0) == parse_iso(b).replace(microsecond=0)


def read_csv(path: Path, required: set[str] | None = None) -> Iterator[dict[str, str]]:
    """Rows of a CSV. With `required`, a file missing any of those columns is
    refused (ValueError) before a row is read."""
    csv.field_size_limit(1 << 30)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = sorted((required or set()) - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"{path} is missing column(s) {', '.join(missing)}; is it the right export?")
        yield from reader


def shopify_by_email(path: Path) -> dict[str, dict[str, str]]:
    """Shopify customers export rows by lowercased email (customers without an
    email can't be matched). Raises if an email is on more than one customer."""
    out: dict[str, dict[str, str]] = {}
    for row in read_csv(path, SHOPIFY_COLUMNS):
        email = (row.get("email") or "").strip().lower()
        if not email:
            continue
        if row.get("email_marketing") not in SHOPIFY_STATES:
            raise ValueError(f"Shopify customer {row.get('customer_id')} has email state "
                             f"{row.get('email_marketing')!r}, which isn't one of {', '.join(sorted(SHOPIFY_STATES))}.")
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
    suppression on a profile that isn't subscribed makes it `unsubscribed`.
    Raises ValueError for a consent or suppressions value it can't read."""
    if row.get("consent") not in KLAVIYO_CONSENTS:
        raise ValueError(f"Klaviyo profile {row.get('id')} has consent {row.get('consent')!r}, "
                         f"which isn't one of {', '.join(sorted(KLAVIYO_CONSENTS))}.")
    try:
        suppressions = json.loads(row.get("suppressions") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Klaviyo profile {row.get('id')}: suppressions aren't valid JSON ({exc}).") from exc
    if not isinstance(suppressions, list) or not all(isinstance(x, dict) for x in suppressions):
        raise ValueError(f"Klaviyo profile {row.get('id')}: suppressions aren't a list of suppressions.")
    strong = [s for s in suppressions if s.get("reason") != "UNSUBSCRIBE"]
    migrated = _property(row, "migrated_from") == "ca"
    if strong:
        state = "suppressed"
        date = (migrated and _property(row, "ca_suppression_timestamp")) or strong[0].get("timestamp") \
            or row.get("suppression_timestamp") or ""
    else:
        state = {"SUBSCRIBED": "subscribed", "UNSUBSCRIBED": "unsubscribed"}.get(row["consent"], "never")
        unsubscribe = [s for s in suppressions if s.get("reason") == "UNSUBSCRIBE"]
        if state == "never" and unsubscribe:
            state = "unsubscribed"
        date = (migrated and _property(row, "ca_consent_timestamp")) or row.get("consent_timestamp") or ""
        if not date and state == "unsubscribed" and unsubscribe:
            date = unsubscribe[0].get("timestamp") or ""  # the unsubscribe's own date when consent has none
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
    sync_time: int = 0  # writes dated at the sync time (D16)


def plan(klaviyo_path: Path, shopify_path: Path, *, now: datetime, only: set[str] | None = None) -> Plan:
    """Work out every write. `only` limits the plan to these emails (trials)."""
    shop = shopify_by_email(shopify_path)
    result = Plan()
    for row in read_csv(klaviyo_path, KLAVIYO_COLUMNS):
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
            newer = bool(sh_date and date and parse_iso(sh_date) >= parse_iso(date))
            result.shopify_newer += newer
            sync_time = newer or not date  # D16
            result.sync_time += sync_time
            result.writes.append({**base, "klaviyo_state": k.state, "shopify_state": sh_state,
                                  "target_state": TARGET[k.state], "target_date": "" if sync_time else date,
                                  "date_rule": DATE_SYNC_TIME if sync_time else DATE_ORIGINAL,
                                  "klaviyo_date": date, "shopify_date": sh_date, "shopify_newer": newer})
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


def written_targets(results_paths: list[Path]) -> dict[str, tuple[str, str, str]]:
    """Shopify customer ID → (state, date, email) for every customer a sync run
    wrote; the date is the one sent (an original date or the sync time). The
    latest result per customer wins."""
    out: dict[str, tuple[str, str, str]] = {}
    for path in results_paths:
        for row in read_csv(path, {"shopify_customer_id", "email", "target_state", "target_date", "outcome"}):
            if row["outcome"] == "written":
                out[row["shopify_customer_id"]] = (row["target_state"], row["target_date"], row["email"])
            else:
                out.pop(row["shopify_customer_id"], None)
    return out


def validate(
    klaviyo_path: Path, shopify_path: Path, *, written: dict[str, tuple[str, str, str]] | None = None,
    d14_emails: set[str] | None = None, klaviyo_before: Path | None = None,
) -> Validation:
    """Check post-run exports against the rules.

    - Every matched customer: its action must be `match` or `d12`; `write` or
      `d14` means it still differs.
    - Every customer the run wrote must still be in the Shopify export, under
      the same email, in the state written and with the date written (dates
      are only set with a state change, pilot P8b).
    - Every D14 email must have a Klaviyo profile and a Shopify customer, both
      subscribed, with Klaviyo's date equal to Shopify's.
    - With `klaviyo_before`, every change to a Klaviyo profile's consent
      state, date or method (or a profile gone) is listed for review."""
    shop = shopify_by_email(shopify_path)
    shop_by_id = {row["customer_id"]: row for row in shop.values()}
    written = written or {}
    d14_emails = d14_emails or set()
    v = Validation()
    after: dict[str, KlaviyoConsent] = {}
    seen_d14: set[str] = set()
    for row in read_csv(klaviyo_path, KLAVIYO_COLUMNS):
        k = klaviyo_consent(row)
        after[k.profile_id] = k
        s = shop.get(k.email) if k.email else None
        if not s:
            continue
        sh_state = s["email_marketing"]
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
        if k.email in d14_emails:
            seen_d14.add(k.email)
            if (k.state, sh_state) != ("subscribed", "SUBSCRIBED"):
                problems.append(f"D14: expected subscribed on both sides, Klaviyo {k.state}, Shopify {sh_state}")
            elif not s.get("email_consent_updated") or not same_second(k.consent_timestamp, s["email_consent_updated"]):
                problems.append(f"D14: Klaviyo date {k.consent_timestamp}, Shopify date {s.get('email_consent_updated')}")
        if problems:
            v.counts["mismatched"] += 1
            v.mismatches.append({**base, "problem": "; ".join(problems)})
        else:
            v.counts["ok"] += 1
    # Customers the run wrote: present, same identity, as written.
    for cid, (state, date, email) in written.items():
        s = shop_by_id.get(cid)
        base = {"email": email, "shopify_customer_id": cid, "klaviyo_profile_id": ""}
        problem = None
        if s is None:
            problem = "written by the run, but not in the post-run Shopify export"
        elif (s.get("email") or "").strip().lower() != email:
            problem = f"written by the run for {email}, but the Shopify customer's email is now {s.get('email')}"
        elif s["email_marketing"] != state:
            problem = f"Shopify {s['email_marketing']}, the run wrote {state}"
        elif date and not same_second(s.get("email_consent_updated"), date):
            problem = f"Shopify date {s.get('email_consent_updated')}, the run wrote {date}"
        v.counts["written_checked"] += 1
        if problem:
            v.counts["written_mismatched"] += 1
            v.mismatches.append({**base, "problem": problem})
    for email in sorted(d14_emails - seen_d14):
        v.counts["mismatched"] += 1
        v.mismatches.append({"email": email, "shopify_customer_id": "", "klaviyo_profile_id": "",
                             "problem": "D14: no matching Klaviyo profile and Shopify customer in the post-run exports"})
    if klaviyo_before:
        for row in read_csv(klaviyo_before, KLAVIYO_COLUMNS):
            b = klaviyo_consent(row)
            a = after.get(b.profile_id)
            if not a:
                v.counts["klaviyo_missing"] += 1
                v.klaviyo_changes.append({"email": b.email, "klaviyo_profile_id": b.profile_id, "field": "profile",
                                          "before": "present", "after": "missing (deleted or merged)"})
                continue
            for name, before, now in (("state", b.state, a.state), ("consent_timestamp", b.consent_timestamp,
                                      a.consent_timestamp), ("method", b.method, a.method)):
                if before != now and not (name == "consent_timestamp" and same_second(before, now)):
                    v.counts["klaviyo_changed_d14" if a.email in d14_emails else "klaviyo_changed"] += 1
                    v.klaviyo_changes.append({"email": a.email, "klaviyo_profile_id": a.profile_id, "field": name,
                                              "before": before, "after": now})
    return v
