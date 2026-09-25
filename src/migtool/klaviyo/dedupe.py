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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from migtool.klaviyo.imports import Importer
from migtool.klaviyo.writes import MIGRATED_FROM, finite_number, identity, property_column, typed_value

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


def plan(
    role: Role, rows: list[dict[str, str]], types: dict[str, str], run_id: str,
    existing: Callable[[list[str], list[str]], set[str]],
    migrated: Callable[[list[str]], set[str]] = lambda emails: set(),
) -> Plan:
    """Parse every row and look up which profiles exist (a read), so the
    confirmation shows exactly what will happen. `existing(emails, phones)`
    returns the identities that already have a profile; `migrated(emails)`
    those whose profile this migration created (tagged `migrated_from=ca`).

    For `suppress`, "existing" means an existing *US* profile: a profile the
    migration created on an earlier run is handled as new again, so re-running
    02 never puts CA-only profiles on the Updated US Profiles list."""
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
    result.existing = existing(emails, phones) if (emails or phones) else set()
    if role.name == "suppress" and result.existing:
        result.existing -= migrated(sorted(e for e in result.existing if "@" in e))
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
