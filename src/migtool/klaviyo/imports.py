"""`profiles import`, `suppressions import` and `lists add`.

Each reads a CSV, sends it in batches, waits for Klaviyo's background jobs,
and records per-record errors in the run's errors file. Every step is safe to
repeat.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from migtool.klaviyo.writes import (
    CONSENT_COLUMNS,
    MIGRATED_FROM,
    Job,
    Writer,
    ca_properties,
    profile_attributes,
)
from migtool.output import CsvWriter, Run
from migtool.runlog import RunLog

SKIPPED_COLUMNS = ["email", "reason"]
IMPORT_WAIT = 60 * 60  # seconds to wait for import jobs


def read_rows(path: Path, *, limit: int | None = None) -> tuple[list[str], list[dict[str, str]]]:
    csv.field_size_limit(1 << 30)
    # utf-8-sig: Excel's "CSV UTF-8" starts the file with a byte-order mark.
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "email" not in reader.fieldnames:
            raise ValueError(f"{path} has no 'email' column.")
        rows = []
        for row in reader:
            rows.append({k: (v or "").strip() for k, v in row.items() if k is not None})
            if limit is not None and len(rows) >= limit:
                break
    return list(reader.fieldnames), rows


@dataclass
class Batch:
    """Rows with a usable email, and the skipped ones with their reason."""

    rows: list[dict[str, str]]
    skipped: list[tuple[str, str]]


def usable(rows: list[dict[str, str]]) -> Batch:
    """Drop rows without an email and repeated emails (the first row wins)."""
    seen: set[str] = set()
    keep, skipped = [], []
    for row in rows:
        email = row["email"].lower()
        if not email:
            skipped.append(("", f"no email (phone {row.get('phone_number') or 'none'})"))
        elif '"' in email or "@" not in email:
            skipped.append((row["email"], "not a valid email"))
        elif email in seen:
            skipped.append((row["email"], "duplicate email in file"))
        else:
            seen.add(email)
            row["email"] = email
            keep.append(row)
    return Batch(keep, skipped)


def write_skipped(run: Run, skipped: list[tuple[str, str]]) -> Path | None:
    if not skipped:
        return None
    path = run.path(".skipped.csv")
    writer = CsvWriter(path, SKIPPED_COLUMNS)
    for email, reason in skipped:
        writer.write({"email": email, "reason": reason})
    writer.close()
    return path


class Importer:
    """Runs the steps of one import against one account.

    `steps` counts what each step sent. The run log's `written` count is the
    command's main step only (`main_step`), so the summary answers "how many
    rows did this command apply". Every row that fails, is refused, or ends
    with an unknown outcome is logged in the errors file."""

    def __init__(
        self, writer: Writer, log: RunLog, *, echo: Callable[[str], None],
        save_job: Callable[[Job], None], main_step: str,
    ) -> None:
        self.w = writer
        self.log = log
        self.echo = echo
        self.save_job = save_job
        self.main_step = main_step
        self.steps: dict[str, int] = {}
        self.unfinished: list[Job] = []

    def _count(self, step: str, n: int) -> None:
        self.steps[step] = self.steps.get(step, 0) + n
        if step == self.main_step:
            self.log.written(n)

    def _refused(self, rejected: list[tuple[str, str]], stage: str) -> None:
        for who, reason in rejected:
            self.log.error(who, f"refused by Klaviyo: {reason}", stage=stage)

    def attributes(self, rows: list[dict[str, str]], extra) -> tuple[list[dict], list[dict[str, str]]]:
        """Profile payloads for `rows`; rows with an unreadable cell are logged
        and dropped. Returns (payloads, rows kept)."""
        payloads, kept = [], []
        for row in rows:
            try:
                payloads.append(profile_attributes(row, extra(row)))
                kept.append(row)
            except ValueError as exc:
                self.log.error(row["email"], f"not sent: {exc}", stage="read")
        return payloads, kept

    def import_profiles(self, profiles: list[dict[str, Any]], *, list_id: str | None, stage: str) -> set[str]:
        """Bulk import and wait. Returns the emails whose import is confirmed;
        failed, refused and unknown ones are logged."""
        if not profiles:
            return set()
        jobs, rejected = self.w.import_profiles(profiles, list_id=list_id)
        self._refused(rejected, stage)
        for job in jobs:
            self.save_job(job)
        self.echo(f"{stage}: {len(profiles):,} profiles in {len(jobs)} job(s)")
        pending = self.w.wait(jobs, timeout=IMPORT_WAIT, progress=self.echo)
        self.unfinished += pending
        ok: set[str] = set()
        for job in jobs:
            done, failed, unknown = self.w.import_result(job)
            ok |= done
            for who, message in failed:
                self.log.error(who, message, stage=stage)
            for who in sorted(unknown):
                self.log.error(who, f"outcome unknown: job {job.id} is {job.status or 'processing'}; "
                               "check the profile in Klaviyo", stage=stage)
        self._count(stage, len(ok))
        return ok

    def subscribe(self, rows: list[dict[str, str]], *, list_id: str) -> None:
        """Historical-import subscribe with each row's original consent timestamp."""
        ready = []
        for row in rows:
            if row.get("consent_timestamp"):
                ready.append((row["email"], row["consent_timestamp"]))
            else:
                self.log.error(row["email"], "SUBSCRIBED without consent_timestamp; not subscribed",
                               stage="subscribe")
        if not ready:
            return
        rejected = self.w.subscribe(ready, list_id=list_id)
        self._refused(rejected, "subscribe")
        self._count("subscribe", len(ready) - len(rejected))
        self.echo(f"subscribe: {len(ready) - len(rejected):,} profiles (historical import, original timestamps)")

    def unsubscribe(self, emails: list[str], *, step: str = "unsubscribe") -> None:
        if not emails:
            return
        rejected = self.w.unsubscribe(emails)
        self._refused(rejected, step)
        self._count(step, len(emails) - len(rejected))
        self.echo(f"{step}: {len(emails) - len(rejected):,} profiles"
                  + (" (as unsubscribe)" if step == "suppress" else ""))

    def suppress(self, emails: list[str], *, as_unsubscribe: bool = False) -> None:
        """Submit suppression jobs, or with `as_unsubscribe` unsubscribe instead
        (immediate, but a later subscribe lifts it).

        Suppression jobs aren't waited on: in the sandbox they took about four
        hours to apply, while their status stayed `processing` and reported
        every profile as skipped. `suppressions check` confirms the result."""
        if not emails:
            return
        if as_unsubscribe:
            self.unsubscribe(emails, step="suppress")
            return
        jobs, rejected = self.w.suppress(emails)
        self._refused(rejected, "suppress")
        for job in jobs:
            self.save_job(job)
        sent = len(emails) - len(rejected)
        self._count("suppress", sent)
        self.echo(
            f"suppress: {sent:,} profiles submitted in {len(jobs)} job(s). Klaviyo can take hours to "
            "apply them, and the job status isn't reliable; run `klaviyo suppressions check` later."
        )

    def skip_unconfirmed(self, rows: list[dict[str, str]], ok: set[str], step: str) -> list[dict[str, str]]:
        """Rows whose profile write is confirmed. The others were already logged;
        their consent isn't touched, so a missing profile is never created bare."""
        kept = [r for r in rows if r["email"] in ok]
        if len(kept) < len(rows):
            self.steps[f"{step}_held_back"] = len(rows) - len(kept)
            self.echo(f"{step}: {len(rows) - len(kept):,} rows held back because their profile write "
                      "wasn't confirmed (see the errors file)")
        return kept


def suppression_reasons(row: dict[str, str]) -> list[str]:
    """Every suppression reason a row carries: all of them from the `suppressions`
    JSON column when present, otherwise the single `suppression_reason`."""
    text = row.get("suppressions", "")
    if text:
        try:
            items = json.loads(text)
            return [str(i.get("reason", "")).upper() for i in items if isinstance(i, dict) and i.get("reason")]
        except ValueError:
            pass
    reason = row.get("suppression_reason", "").upper()
    return [reason] if reason else []


def plan_profiles_import(rows: list[dict[str, str]]) -> dict[str, list]:
    """Which consent step each row needs after the bulk import. Any suppression
    other than an unsubscribe (hard bounce, spam complaint, …) is suppressed,
    even when a newer unsubscribe is the latest reason."""
    plan: dict[str, list] = {"subscribe": [], "unsubscribe": [], "suppress": []}
    for row in rows:
        consent = row.get("consent", "").upper()
        reasons = suppression_reasons(row)
        if consent == "SUBSCRIBED" and not reasons:
            plan["subscribe"].append(row)
        elif consent == "UNSUBSCRIBED" or "UNSUBSCRIBE" in reasons:
            plan["unsubscribe"].append(row["email"])
        if any(r != "UNSUBSCRIBE" for r in reasons):
            plan["suppress"].append(row["email"])
    return plan


def profiles_import(
    imp: Importer, rows: list[dict[str, str]], *, list_id: str, run_id: str, as_unsubscribe: bool = False
) -> None:
    """Import CA profiles: fields and `ca_*` properties onto every profile, all of
    them into `list_id`, then consent from the file, for confirmed imports only.
    Never-subscribed rows get no consent change."""
    profiles, rows = imp.attributes(rows, lambda r: ca_properties(r, run_id))
    ok = imp.import_profiles(profiles, list_id=list_id, stage="import")
    plan = plan_profiles_import(imp.skip_unconfirmed(rows, ok, "consent"))
    imp.subscribe(plan["subscribe"], list_id=list_id)
    imp.unsubscribe(plan["unsubscribe"])
    imp.suppress(plan["suppress"], as_unsubscribe=as_unsubscribe)


def _tag(run_id: str) -> dict[str, str]:
    return {"migrated_from": MIGRATED_FROM, "migration_run_id": run_id}


def suppressions_import(
    imp: Importer, rows: list[dict[str, str]], *, run_id: str, as_unsubscribe: bool = False
) -> int:
    """Suppress every email. Emails with no profile get one first, tagged as
    migrated, then suppressed. Suppression still goes ahead for an email whose
    profile couldn't be created (Klaviyo creates it untagged): blocking email
    matters more than the tag. Returns the number of profiles created."""
    existing = imp.w.existing_emails(r["email"] for r in rows)
    missing = [r for r in rows if r["email"] not in existing]
    payloads, _ = imp.attributes(missing, lambda r: {"ca_external_id": r.get("external_id"), **_tag(run_id)})
    created = imp.import_profiles(payloads, list_id=None, stage="create")
    imp.suppress([r["email"] for r in rows], as_unsubscribe=as_unsubscribe)
    return len(created)


def lists_add(
    imp: Importer, rows: list[dict[str, str]], columns: list[str], *, list_id: str, run_id: str
) -> int:
    """Add every row to `list_id`. Existing profiles are only added (no field or
    consent change); missing ones are created from the file's fields and tagged.
    With consent columns, SUBSCRIBED rows whose add is confirmed are also
    subscribed with their original timestamp. Returns the number created."""
    existing = imp.w.existing_emails(r["email"] for r in rows)
    missing = [r for r in rows if r["email"] not in existing]
    present = [r for r in rows if r["email"] in existing]
    payloads, _ = imp.attributes(missing, lambda r: {"ca_external_id": r.get("external_id"), **_tag(run_id)})
    created = imp.import_profiles(payloads, list_id=list_id, stage="add")
    added = imp.import_profiles([{"email": r["email"]} for r in present], list_id=list_id, stage="add")
    if CONSENT_COLUMNS <= set(columns):
        confirmed = imp.skip_unconfirmed(rows, created | added, "subscribe")
        imp.subscribe(plan_profiles_import(confirmed)["subscribe"], list_id=list_id)
    return len(created)
