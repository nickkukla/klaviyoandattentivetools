"""`profiles import`, `suppressions import` and `lists add`.

Each reads a CSV, sends it in batches, waits for Klaviyo's background jobs,
and records per-record errors in the run's errors file. Every step is safe to
repeat.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from migtool.klaviyo.writes import (
    CONSENT_COLUMNS,
    IMPORT_BATCH,
    MIGRATED_FROM,
    SUBSCRIBE_BATCH,
    SUPPRESS_BATCH,
    UNSUBSCRIBE_BATCH,
    Job,
    Writer,
    batches,
    ca_properties,
    profile_attributes,
)
from migtool.output import CsvWriter, Run
from migtool.runlog import RunLog

SKIPPED_COLUMNS = ["email", "reason"]
IMPORT_WAIT = 60 * 60  # seconds to wait for import jobs


def read_rows(path: Path, *, limit: int | None = None) -> tuple[list[str], list[dict[str, str]]]:
    csv.field_size_limit(1 << 30)
    with open(path, newline="", encoding="utf-8") as f:
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
    rows did this command apply"."""

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

    def _run_jobs(self, jobs: list[Job], stage: str, timeout: float) -> list[Job]:
        pending = self.w.wait(jobs, timeout=timeout, progress=self.echo)
        self.unfinished += pending
        for job in pending:
            self.echo(f"  {stage} job {job.id} still {job.status or 'processing'} after waiting")
        return [j for j in jobs if j not in pending]

    def import_profiles(self, profiles: list[dict[str, Any]], *, list_id: str | None, stage: str) -> None:
        """Bulk import, wait, and record per-record errors."""
        if not profiles:
            return
        jobs = []
        for chunk in batches(profiles, IMPORT_BATCH):
            job = self.w.import_profiles(chunk, list_id=list_id)
            self.save_job(job)
            jobs.append(job)
        self.echo(f"{stage}: {len(profiles):,} profiles in {len(jobs)} job(s)")
        for job in self._run_jobs(jobs, stage, IMPORT_WAIT):
            errors = self.w.import_errors(job)
            for who, message in errors:
                self.log.error(who, message, stage=stage)
            self._count(stage, max(0, job.size - len(errors)))

    def subscribe(self, rows: list[dict[str, str]], *, list_id: str) -> None:
        """Historical-import subscribe with each row's original consent timestamp."""
        ready = []
        for row in rows:
            if row.get("consent_timestamp"):
                ready.append((row["email"], row["consent_timestamp"]))
            else:
                self.log.error(row["email"], "SUBSCRIBED without consent_timestamp; not subscribed",
                               stage="subscribe")
        for chunk in batches(ready, SUBSCRIBE_BATCH):
            self.w.subscribe(chunk, list_id=list_id)
        self._count("subscribe", len(ready))
        if ready:
            self.echo(f"subscribe: {len(ready):,} profiles (historical import, original timestamps)")

    def unsubscribe(self, emails: list[str]) -> None:
        for chunk in batches(emails, UNSUBSCRIBE_BATCH):
            self.w.unsubscribe(chunk)
        self._count("unsubscribe", len(emails))
        if emails:
            self.echo(f"unsubscribe: {len(emails):,} profiles")

    def suppress(self, emails: list[str], *, as_unsubscribe: bool = False) -> None:
        """Submit suppression jobs, or with `as_unsubscribe` unsubscribe instead
        (immediate, but a later subscribe lifts it).

        Suppression jobs aren't waited on: in the sandbox they took about four
        hours to apply, while their status stayed `processing` and reported
        every profile as skipped. `suppressions check` confirms the result."""
        if not emails:
            return
        if as_unsubscribe:
            for chunk in batches(emails, UNSUBSCRIBE_BATCH):
                self.w.unsubscribe(chunk)
            self._count("suppress", len(emails))
            self.echo(f"suppress (as unsubscribe): {len(emails):,} profiles")
            return
        jobs = 0
        for chunk in batches(emails, SUPPRESS_BATCH):
            self.save_job(self.w.suppress(chunk))
            jobs += 1
        self._count("suppress", len(emails))
        self.echo(
            f"suppress: {len(emails):,} profiles submitted in {jobs} job(s). Klaviyo can take hours to "
            "apply them, and the job status isn't reliable; run `klaviyo suppressions check` later."
        )


def plan_profiles_import(rows: list[dict[str, str]]) -> dict[str, list]:
    """Which consent step each row needs after the bulk import."""
    plan: dict[str, list] = {"subscribe": [], "unsubscribe": [], "suppress": []}
    for row in rows:
        consent = row.get("consent", "").upper()
        reason = row.get("suppression_reason", "").upper()
        if consent == "SUBSCRIBED" and not reason:
            plan["subscribe"].append(row)
        elif consent == "UNSUBSCRIBED" or reason == "UNSUBSCRIBE":
            plan["unsubscribe"].append(row["email"])
        if reason and reason != "UNSUBSCRIBE":
            plan["suppress"].append(row["email"])
    return plan


def profiles_import(
    imp: Importer, rows: list[dict[str, str]], *, list_id: str, run_id: str, as_unsubscribe: bool = False
) -> None:
    """Import CA profiles: fields and `ca_*` properties onto every profile, all of
    them into `list_id`, then consent from the file. Never-subscribed rows get
    no consent change."""
    profiles = [profile_attributes(r, ca_properties(r, run_id)) for r in rows]
    imp.import_profiles(profiles, list_id=list_id, stage="import")
    plan = plan_profiles_import(rows)
    imp.subscribe(plan["subscribe"], list_id=list_id)
    imp.unsubscribe(plan["unsubscribe"])
    imp.suppress(plan["suppress"], as_unsubscribe=as_unsubscribe)


def _tag(run_id: str) -> dict[str, str]:
    return {"migrated_from": MIGRATED_FROM, "migration_run_id": run_id}


def suppressions_import(
    imp: Importer, rows: list[dict[str, str]], *, run_id: str, as_unsubscribe: bool = False
) -> int:
    """Suppress every email. Emails with no profile get one first, tagged as
    migrated, then suppressed. Returns the number of profiles created."""
    existing = imp.w.existing_emails(r["email"] for r in rows)
    missing = [r for r in rows if r["email"] not in existing]
    imp.import_profiles(
        [profile_attributes(r, {"ca_external_id": r.get("external_id"), **_tag(run_id)}) for r in missing],
        list_id=None, stage="create",
    )
    imp.suppress([r["email"] for r in rows], as_unsubscribe=as_unsubscribe)
    return len(missing)


def lists_add(
    imp: Importer, rows: list[dict[str, str]], columns: list[str], *, list_id: str, run_id: str
) -> int:
    """Add every row to `list_id`. Existing profiles are only added (no field or
    consent change); missing ones are created from the file's fields and tagged.
    With consent columns, SUBSCRIBED rows are also subscribed with their
    original timestamp. Returns the number of profiles created."""
    existing = imp.w.existing_emails(r["email"] for r in rows)
    missing = [r for r in rows if r["email"] not in existing]
    present = [r for r in rows if r["email"] in existing]
    imp.import_profiles(
        [profile_attributes(r, {"ca_external_id": r.get("external_id"), **_tag(run_id)}) for r in missing],
        list_id=list_id, stage="add",
    )
    imp.import_profiles([{"email": r["email"]} for r in present], list_id=list_id, stage="add")
    if CONSENT_COLUMNS <= set(columns):
        imp.subscribe(plan_profiles_import(rows)["subscribe"], list_id=list_id)
    return len(missing)
