"""Import the dedupe files: CSVs in Klaviyo's UI-import layout, one role per file.

The files come from the external dedupe (see docs/DEDUPE_IMPORT.md) and differ
from the `profiles export` layout the other commands read:

- `Email Marketing Consent` (`Subscribe` / `Unsubscribed` / blank) is the consent
  instruction, with `Email Marketing Consent Timestamp` or `ca_consent_timestamp`
  as the original date. `ca_consent` is audit data only, never an instruction.
- `location_<field>` columns are Klaviyo location fields.
- Every other column is a custom property under its own name, with no
  `properties.` prefix and no type suffix. Types are recovered from the header
  of a `profiles export` file (`--types-from`); `migration_hold` is a boolean.
- `migration_source` is replaced by the tool's own `migrated_from=ca` tag, and
  `ca_consent_method_detail` is written as `ca_consent_source`.
- Rows are identified by email, or by phone number when there's no email.

Each role fixes what an import may do (see ROLES).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from migtool.klaviyo.imports import Importer
from migtool.klaviyo.writes import MIGRATED_FROM, finite_number, identity, property_column, typed_value
from migtool.output import parse_iso

CONSENT = "Email Marketing Consent"
CONSENT_TIMESTAMP = "Email Marketing Consent Timestamp"
FALLBACK_TIMESTAMP = "ca_consent_timestamp"
FIELDS = ["first_name", "last_name", "organization", "title", "locale", "image"]
NUMERIC_LOCATION = {"latitude", "longitude"}
RENAMED = {"ca_consent_method_detail": "ca_consent_source"}
DROPPED = {"migration_source"}  # replaced by the migrated_from tag
BOOLEANS = {"migration_hold"}
HOLD = "migration_hold"


@dataclass(frozen=True)
class Role:
    name: str
    files: str  # which dedupe files use it, for help and messages
    creates: bool  # False: update existing profiles only, never create
    join_list: bool  # takes --join-list
    consent: bool  # applies Email Marketing Consent
    tags: bool  # adds migrated_from / migration_run_id
    only_hold: bool = False  # sends migration_hold and nothing else


ROLES: dict[str, Role] = {r.name: r for r in (
    Role("hold", "01, 05", creates=False, join_list=False, consent=False, tags=False, only_hold=True),
    Role("hold-new", "01b", creates=True, join_list=False, consent=False, tags=True),
    Role("suppress", "02", creates=True, join_list=True, consent=False, tags=True),
    Role("new", "03a-03d", creates=True, join_list=True, consent=True, tags=True),
    Role("kept", "04a, 04b", creates=False, join_list=True, consent=True, tags=True),
)}


def type_map(path: Path) -> dict[str, str]:
    """Property name → type (`number`, `bool`, `json`, `text`) from the header of
    a `profiles export` CSV."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    return dict(property_column(c) for c in header if c.startswith("properties."))


def parse_bool(text: str) -> bool:
    if text.strip().lower() not in ("true", "false"):
        raise ValueError(f"'{text}' is not true or false")
    return text.strip().lower() == "true"


def consent_instruction(row: dict[str, str]) -> str | None:
    """`SUBSCRIBED`, `UNSUBSCRIBED` or None (no change) from Email Marketing Consent."""
    text = (row.get(CONSENT) or "").strip().lower()
    if not text:
        return None
    if text in ("subscribe", "subscribed"):
        return "SUBSCRIBED"
    if text in ("unsubscribe", "unsubscribed"):
        return "UNSUBSCRIBED"
    raise ValueError(f"{CONSENT}: '{row.get(CONSENT)}' is not Subscribe or Unsubscribed")


def consent_timestamp(row: dict[str, str]) -> str:
    return row.get(CONSENT_TIMESTAMP) or row.get(FALLBACK_TIMESTAMP) or ""


def payload(row: dict[str, str], types: dict[str, str], extra: dict[str, Any], *, only_hold: bool) -> dict[str, Any]:
    """Profile attributes for one row. Blank cells are left out, so they never
    clear a destination value. Raises ValueError naming the bad column."""
    attrs: dict[str, Any] = {}
    if row.get("email"):
        attrs["email"] = row["email"]
    if row.get("phone_number"):
        attrs["phone_number"] = row["phone_number"]
    props: dict[str, Any] = {}
    handled = {"email", "phone_number", CONSENT, CONSENT_TIMESTAMP, *FIELDS, *DROPPED}
    for column, text in row.items():
        if not text or column in handled:
            continue
        if only_hold and column != HOLD:
            continue
        try:
            if column.startswith("location_"):
                key = column.removeprefix("location_")
                attrs.setdefault("location", {})[key] = (
                    float(finite_number(text)) if key in NUMERIC_LOCATION else text
                )
            elif column.startswith("$"):
                continue  # Klaviyo-internal
            elif column in BOOLEANS:
                props[column] = parse_bool(text)
            else:
                props[RENAMED.get(column, column)] = typed_value(text, types.get(column, "text"))
        except ValueError as exc:
            raise ValueError(f"{column}: {exc}") from exc
    if not only_hold:
        for f in FIELDS:
            if row.get(f):
                attrs[f] = row[f]
    props.update(extra)
    if props:
        attrs["properties"] = props
    return attrs


@dataclass
class Plan:
    """What an import will do, worked out before anything is written."""

    role: Role
    rows: list[dict[str, str]] = field(default_factory=list)  # rows to send
    payloads: list[dict[str, Any]] = field(default_factory=list)
    instructions: list[str | None] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (identity, reason)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    existing: set[str] = field(default_factory=set)  # identities with a profile

    @property
    def updates(self) -> int:
        return sum(1 for r in self.rows if identity(r) in self.existing)

    @property
    def creates(self) -> int:
        return len(self.rows) - self.updates

    def count(self, instruction: str) -> int:
        return sum(1 for i in self.instructions if i == instruction)


def usable(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    """Rows identified by email, or phone when there's no email. Emails are
    lowercased; rows with neither, or repeated in the file, are skipped."""
    seen: set[str] = set()
    keep, skipped = [], []
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        phone = (row.get("phone_number") or "").strip()
        row["email"], row["phone_number"] = email, phone
        if email and ('"' in email or "@" not in email):
            skipped.append((email, "not a valid email"))
            continue
        if not email and not phone:
            skipped.append(("", "no email or phone number"))
            continue
        if phone and not email and not phone.startswith("+"):
            skipped.append((phone, "phone number isn't in +international format"))
            continue
        who = identity(row)
        if who in seen:
            skipped.append((who, "duplicate in file"))
            continue
        seen.add(who)
        keep.append(row)
    return keep, skipped


def load_snapshot(path: Path) -> set[str]:
    """Emails (lowercased) and phone numbers in a pre-migration `profiles export`
    of the destination: the profiles that existed before the migration."""
    found: set[str] = set()
    csv.field_size_limit(1 << 30)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if (row.get("email") or "").strip():
                found.add(row["email"].strip().lower())
            if (row.get("phone_number") or "").strip():
                found.add(row["phone_number"].strip())
    return found


def plan(
    role: Role, rows: list[dict[str, str]], types: dict[str, str], run_id: str,
    existing: Callable[[list[str], list[str]], set[str]],
    us_snapshot: set[str] | None = None,
) -> Plan:
    """Parse every row and look up which profiles exist (a read), so the
    confirmation shows exactly what will happen. `existing(emails, phones)`
    returns the identities that already have a profile.

    For `suppress`, "existing" means a *US* profile, taken from `us_snapshot`
    (the pre-migration US export), never from the live account. A profile an
    earlier or partly failed run created (including one the suppression call
    itself created) is still CA-only, so re-running 02 never puts it on the
    Updated US Profiles list."""
    result = Plan(role)
    kept, result.skipped = usable(rows)
    parsed = []
    for row in kept:
        try:
            instruction = consent_instruction(row) if role.consent else None
            if instruction and not row["email"]:
                raise ValueError(f"{CONSENT}: needs an email; phone-only rows can't have email consent")
            parsed.append((row, instruction))
        except ValueError as exc:
            result.unreadable.append((identity(row), str(exc)))
    emails = [r["email"] for r, _ in parsed if r["email"]]
    phones = [r["phone_number"] for r, _ in parsed if not r["email"]]
    if role.name == "suppress":
        if us_snapshot is None:
            raise ValueError("role suppress needs the pre-migration US snapshot")
        result.existing = {identity(r) for r, _ in parsed if identity(r) in us_snapshot}
    else:
        result.existing = existing(emails, phones) if (emails or phones) else set()
    for row, instruction in parsed:
        present = identity(row) in result.existing
        if not role.creates and not present:
            result.skipped.append((identity(row), "no existing profile in the destination (update only)"))
            continue
        # Tags mark profiles the migration changed: new ones always; for
        # `suppress`, only profiles it creates (existing ones are US profiles).
        tagged = role.tags and not (role.name == "suppress" and present)
        extra = {"migrated_from": MIGRATED_FROM, "migration_run_id": run_id} if tagged else {}
        try:
            result.payloads.append(payload(row, types, extra, only_hold=role.only_hold))
        except ValueError as exc:
            result.unreadable.append((identity(row), f"not sent: {exc}"))
            continue
        result.rows.append(row)
        result.instructions.append(instruction)
    return result


def run(imp: Importer, p: Plan, *, join_list: str | None, subscribe_list: str | None) -> None:
    """Carry out a plan: write the profiles, then consent or suppression for
    the ones whose write Klaviyo confirmed."""
    role = p.role
    if role.name == "suppress":
        # Existing profiles (US profiles CA suppression changes) join the list;
        # new ones are created untagged-by-list and suppressed like the rest.
        existing = [(r, a) for r, a in zip(p.rows, p.payloads) if identity(r) in p.existing]
        new = [(r, a) for r, a in zip(p.rows, p.payloads) if identity(r) not in p.existing]
        imp.import_profiles([a for _, a in existing], list_id=join_list, stage="update")
        imp.import_profiles([a for _, a in new], list_id=None, stage="create")
        imp.suppress([r["email"] for r in p.rows if r["email"]])
        return
    stage = "import" if role.creates else "update"
    ok = imp.import_profiles(p.payloads, list_id=join_list if role.join_list else None, stage=stage)
    if not role.consent:
        return
    wanted = [(r, i) for r, i in zip(p.rows, p.instructions) if i]
    confirmed = {identity(r) for r in imp.skip_unconfirmed([r for r, _ in wanted], ok, "consent")}
    subscribe = [{"email": r["email"], "consent_timestamp": consent_timestamp(r)}
                 for r, i in wanted if i == "SUBSCRIBED" and identity(r) in confirmed]
    unsubscribe = [r["email"] for r, i in wanted if i == "UNSUBSCRIBED" and identity(r) in confirmed]
    if subscribe:
        imp.subscribe(subscribe, list_id=subscribe_list)
    imp.unsubscribe(unsubscribe)


# --- Checking an import (read-only) -----------------------------------------------

CHECK_COLUMNS = ["identity", "problems"]
PROFILE_FIELDS = "email,phone_number,first_name,last_name,organization,title,locale,image,location,subscriptions,properties"


def list_members(client, list_id: str) -> set[str]:
    """Profile IDs on a list (compared by ID, so it doesn't matter whether a
    row is identified by email or phone)."""
    found: set[str] = set()
    for page in client.paginate(f"/lists/{list_id}/profiles/", tier="L",
                                params={"fields[profile]": "email", "page[size]": "100"}):
        found.update(p["id"] for p in page["data"])
    return found


def fetch_profiles(client, emails: list[str], phones: list[str]) -> dict[str, dict[str, Any]]:
    """The row identity (the email or phone it was *looked up by*) → profile
    attributes, with the profile ID under `_id`. A phone-only row that matches
    a profile which also has an email is still found under its phone."""
    out: dict[str, dict[str, Any]] = {}
    for field_name, values in (("email", sorted(set(emails))), ("phone_number", sorted(set(phones)))):
        for i in range(0, len(values), 100):
            listed = ",".join(json.dumps(v) for v in values[i:i + 100])
            params = {"filter": f"any({field_name},[{listed}])", "additional-fields[profile]": "subscriptions",
                      "fields[profile]": PROFILE_FIELDS, "page[size]": "100"}
            for page in client.paginate("/profiles/", tier="L", params=params):
                for p in page["data"]:
                    key = (p["attributes"].get(field_name) or "")
                    out[key.lower() if field_name == "email" else key] = {**p["attributes"], "_id": p["id"]}
    return out


def _marketing(attrs: dict[str, Any]) -> dict[str, Any]:
    return ((attrs.get("subscriptions") or {}).get("email") or {}).get("marketing") or {}


def _same(expected: Any, actual: Any, *, tolerance: bool = False) -> bool:
    """Equal, type for type, all the way down. Numbers compare exactly by value,
    so 3 and 3.0 match (Klaviyo may return either) but nothing else does.
    `tolerance` allows float rounding, for coordinates only."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if tolerance:
            return abs(expected - actual) <= 1e-9 * max(1.0, abs(expected))
        return expected == actual
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(_same(e, a) for e, a in zip(expected, actual))
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(_same(expected[k], actual[k]) for k in expected)
    return type(expected) is type(actual) and expected == actual


def _field_problems(sent: dict[str, Any], attrs: dict[str, Any]) -> list[str]:
    """Every field and property the import sends, compared with what's stored.
    The migration tags are checked separately (run IDs differ between runs)."""
    found = []
    for key, value in sent.items():
        # The lookup identifier matched by definition; a phone sent alongside an
        # email is a field like any other (Klaviyo drops some invalid numbers).
        if key in ("email", "properties", "location") or (key == "phone_number" and not sent.get("email")):
            continue
        if not _same(value, attrs.get(key)):
            found.append(f"{key} is {attrs.get(key)!r}, expected {value!r}")
    stored_location = attrs.get("location") or {}
    sent_location = sent.get("location") or {}
    for key, value in sent_location.items():
        # Klaviyo recalculates the timezone from the coordinates when they're
        # sent (None when they contradict the country), whatever the import says.
        if key == "timezone" and ("latitude" in sent_location or "longitude" in sent_location):
            continue
        if not _same(value, stored_location.get(key), tolerance=key in ("latitude", "longitude")):
            found.append(f"location.{key} is {stored_location.get(key)!r}, expected {value!r}")
    stored = attrs.get("properties") or {}
    for key, value in (sent.get("properties") or {}).items():
        if key in ("migrated_from", "migration_run_id"):
            continue
        if not _same(value, stored.get(key)):
            found.append(f"property {key} is {stored.get(key)!r}, expected {value!r}")
    return found


def _strong_suppressions(attrs: dict[str, Any]) -> list[str]:
    return [s.get("reason") for s in _marketing(attrs).get("suppression") or [] if s.get("reason") != "UNSUBSCRIBE"]


def problems(
    role: Role, row: dict[str, str], attrs: dict[str, Any] | None, *,
    types: dict[str, str], us_profile: bool, join: set[str] | None, subscribe: set[str] | None,
) -> list[str]:
    """Everything that differs between what one row's import should have
    produced and what's stored. `us_profile`: for `suppress`, whether the row
    is an existing US profile (from the snapshot). Empty means it landed."""
    if attrs is None:
        return ["no profile"]
    found: list[str] = []
    props = attrs.get("properties") or {}
    pid = attrs.get("_id")
    # The fields and properties this role sends.
    found += _field_problems(payload(row, types, {}, only_hold=role.only_hold), attrs)
    # Migration tags: every tagged role, except existing US profiles in 02.
    if role.tags and not (role.name == "suppress" and us_profile) and props.get("migrated_from") != MIGRATED_FROM:
        found.append("missing migrated_from=ca tag")
    # List membership.
    if role.join_list and join is not None and not (role.name == "suppress" and not us_profile):
        if pid not in join:
            found.append("not on the join list")
    # Suppression: rows carrying a CA suppression (02, 03d) must be suppressed.
    reason = (row.get("ca_suppression_reason") or "").strip().upper()
    if role.name == "suppress" or (reason and reason != "UNSUBSCRIBE"):
        if not _strong_suppressions(attrs):
            found.append(f"not suppressed (expected {reason or 'a suppression'})")
    # Consent.
    if role.consent:
        instruction = consent_instruction(row)
        m = _marketing(attrs)
        if instruction == "SUBSCRIBED":
            if m.get("consent") != "SUBSCRIBED":
                found.append(f"consent is {m.get('consent')}, expected SUBSCRIBED")
            if _strong_suppressions(attrs):
                found.append(f"subscribed but still suppressed ({', '.join(_strong_suppressions(attrs))}): "
                             "won't receive email")
            if subscribe is not None and pid not in subscribe:
                found.append("not on the subscribe list")
            # A profile the migration creates (role new) must carry the file's
            # original date. An existing subscriber (role kept) keeps whatever
            # date it already had, earlier or later, so there's nothing to check.
            want, got = consent_timestamp(row), m.get("consent_timestamp")
            if role.name == "new" and want:
                if not got or parse_iso(got).replace(microsecond=0) != parse_iso(want).replace(microsecond=0):
                    found.append(f"consent_timestamp is {got}, expected {want}")
        elif instruction == "UNSUBSCRIBED" and m.get("consent") != "UNSUBSCRIBED":
            found.append(f"consent is {m.get('consent')}, expected UNSUBSCRIBED")
    return found


def check(
    client, role: Role, rows: list[dict[str, str]], *, types: dict[str, str], us_snapshot: set[str] | None,
    join_list: str | None, subscribe_list: str | None, progress: Callable[[str], None] = lambda _: None,
) -> tuple[dict[str, int], list[dict[str, str]], list[tuple[str, str]]]:
    """Check every usable row against its role's rules. Returns (counts, mismatch
    rows for the CSV, skipped rows). Read-only."""
    kept, skipped = usable(rows)
    progress(f"reading {len(kept):,} profiles")
    profiles = fetch_profiles(client, [r["email"] for r in kept if r["email"]],
                             [r["phone_number"] for r in kept if not r["email"]])
    join = subscribe = None
    if join_list:
        progress(f"reading members of list {join_list}")
        join = list_members(client, join_list)
    if subscribe_list:
        progress(f"reading members of list {subscribe_list}")
        subscribe = list_members(client, subscribe_list)
    counts = {"checked": 0, "ok": 0, "mismatched": 0}
    mismatches: list[dict[str, str]] = []
    for row in kept:
        counts["checked"] += 1
        who = identity(row)
        try:
            found = problems(role, row, profiles.get(who), types=types,
                             us_profile=bool(us_snapshot and who in us_snapshot), join=join, subscribe=subscribe)
        except ValueError as exc:
            found = [f"unreadable row: {exc}"]
        if found:
            counts["mismatched"] += 1
            mismatches.append({"identity": who, "problems": "; ".join(found)})
        else:
            counts["ok"] += 1
    return counts, mismatches, skipped
