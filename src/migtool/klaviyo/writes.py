"""Klaviyo writes: CSV rows to API payloads, batched bulk jobs, job tracking.

Shared by `profiles import`, `suppressions import` and `lists add`. The CSV
layout is the one `profiles export` writes; a file with only an `email`
column works too.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from migtool.http import ApiError
from migtool.klaviyo.client import KlaviyoClient

IMPORT_BATCH = 1000  # profiles per bulk import job (limit 10,000 and 5 MB)
SUBSCRIBE_BATCH = 1000  # limit 1,000
UNSUBSCRIBE_BATCH = 100
SUPPRESS_BATCH = 100  # limit 100
LOOKUP_BATCH = 100  # emails per `any(email,[…])` lookup

# Fields copied onto the destination profile. The source `id`, `external_id`
# and `anonymous_id` are never sent.
FIELDS = ["email", "phone_number", "first_name", "last_name", "organization", "title", "locale", "image"]
NUMERIC_LOCATION = {"latitude", "longitude"}
CONSENT_COLUMNS = {"consent", "consent_timestamp"}
MIGRATED_FROM = "ca"
CUSTOM_SOURCE = "migtool CA migration"


def parse_value(text: str) -> Any:
    """Turn an exported cell back into a value: JSON arrays and objects, booleans,
    and numbers whose text is their canonical form (so `01234` stays a string)."""
    if text in ("true", "false"):
        return text == "true"
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return text
    for kind in (int, float):
        try:
            number = kind(text)
        except ValueError:
            continue
        if str(number) == text:
            return number
    return text


def profile_attributes(row: dict[str, str], extra_properties: dict[str, Any]) -> dict[str, Any]:
    """Profile attributes for a bulk import. Blank cells are left out, so they
    never clear a value in the destination."""
    attrs: dict[str, Any] = {k: row[k] for k in FIELDS if row.get(k)}
    location = {
        k.removeprefix("location."): (float(v) if k.removeprefix("location.") in NUMERIC_LOCATION else v)
        for k, v in row.items() if k.startswith("location.") and v
    }
    if location:
        attrs["location"] = location
    props = {
        k.removeprefix("properties."): parse_value(v)
        for k, v in row.items()
        # `$…` keys are Klaviyo-internal (e.g. `$consent`) and must not be written.
        if k.startswith("properties.") and v and not k.startswith("properties.$")
    }
    props.update({k: v for k, v in extra_properties.items() if v not in (None, "")})
    if props:
        attrs["properties"] = props
    return attrs


def ca_properties(row: dict[str, str], run_id: str) -> dict[str, Any]:
    """The `ca_*` and migration tag properties for a CA profile row."""
    return {
        "ca_external_id": row.get("external_id"),
        "ca_consent_method": row.get("method"),
        "ca_consent_source": row.get("method_detail") or row.get("custom_method_detail"),
        "ca_suppression_reason": row.get("suppression_reason"),
        "ca_suppression_timestamp": row.get("suppression_timestamp"),
        "migrated_from": MIGRATED_FROM,
        "migration_run_id": run_id,
    }


def batches(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


@dataclass
class Job:
    kind: str  # API collection, e.g. "profile-bulk-import-jobs"
    id: str
    size: int
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.attributes.get("status", "")

    @property
    def done(self) -> bool:
        return self.status in ("complete", "cancelled", "failed")


class Writer:
    """Bulk write calls against one Klaviyo account."""

    def __init__(
        self,
        client: KlaviyoClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = 10,
    ) -> None:
        self.client = client
        self._sleep = sleep
        self._clock = clock
        self.poll_seconds = poll_seconds

    def import_profiles(self, profiles: Sequence[dict[str, Any]], *, list_id: str | None) -> Job:
        body: dict[str, Any] = {"data": {
            "type": "profile-bulk-import-job",
            "attributes": {"profiles": {"data": [{"type": "profile", "attributes": p} for p in profiles]}},
        }}
        if list_id:
            body["data"]["relationships"] = {"lists": {"data": [{"type": "list", "id": list_id}]}}
        data = self.client.post("/profile-bulk-import-jobs/", body, tier="M")["data"]
        return Job("profile-bulk-import-jobs", data["id"], len(profiles), data["attributes"])

    def subscribe(self, rows: Sequence[tuple[str, str]], *, list_id: str) -> None:
        """Historical-import subscribe: (email, consented_at) pairs. No job to track."""
        self.client.post("/profile-subscription-bulk-create-jobs/", {"data": {
            "type": "profile-subscription-bulk-create-job",
            "attributes": {
                "custom_source": CUSTOM_SOURCE,
                "historical_import": True,
                "profiles": {"data": [
                    {"type": "profile", "attributes": {"email": email, "subscriptions": {
                        "email": {"marketing": {"consent": "SUBSCRIBED", "consented_at": at}}}}}
                    for email, at in rows
                ]},
            },
            "relationships": {"list": {"data": {"type": "list", "id": list_id}}},
        }}, tier="L")

    def unsubscribe(self, emails: Sequence[str]) -> None:
        """Unsubscribe from email marketing. No job to track."""
        self.client.post("/profile-subscription-bulk-delete-jobs/", {"data": {
            "type": "profile-subscription-bulk-delete-job",
            "attributes": {"profiles": {"data": [
                {"type": "profile", "attributes": {"email": e, "subscriptions": {
                    "email": {"marketing": {"consent": "UNSUBSCRIBED"}}}}}
                for e in emails
            ]}},
        }}, tier="L")

    def suppress(self, emails: Sequence[str]) -> Job:
        data = self.client.post("/profile-suppression-bulk-create-jobs/", {"data": {
            "type": "profile-suppression-bulk-create-job",
            "attributes": {"profiles": {"data": [
                {"type": "profile", "attributes": {"email": e}} for e in emails
            ]}},
        }}, tier="L")["data"]
        return Job("profile-suppression-bulk-create-jobs", data["id"], len(emails), data["attributes"])

    def existing_emails(self, emails: Iterable[str]) -> set[str]:
        """Which of `emails` already have a profile (compared lowercased)."""
        found: set[str] = set()
        for chunk in batches(sorted({e.lower() for e in emails}), LOOKUP_BATCH):
            listed = ",".join(json.dumps(e) for e in chunk)
            params = {"filter": f"any(email,[{listed}])", "fields[profile]": "email", "page[size]": "100"}
            for page in self.client.paginate("/profiles/", tier="L", params=params):
                found.update((p["attributes"].get("email") or "").lower() for p in page["data"])
        return found

    def wait(
        self, jobs: list[Job], *, timeout: float, progress: Callable[[str], None] = lambda _: None
    ) -> list[Job]:
        """Poll until every job is done or `timeout` seconds pass. Returns the
        jobs still unfinished."""
        start = self._clock()
        deadline = start + timeout
        pending = [j for j in jobs if not j.done]
        last_report: tuple[int, float] | None = None
        while pending:
            for job in pending:
                job.attributes = self.client.get(f"/{job.kind}/{job.id}/")["data"]["attributes"]
            pending = [j for j in pending if not j.done]
            now = self._clock()
            if not pending or now >= deadline:
                break
            # Report when the count changes, and otherwise once a minute.
            if last_report is None or last_report[0] != len(pending) or now - last_report[1] >= 60:
                progress(f"  waiting on {len(pending)} job(s), {int(now - start)}s so far")
                last_report = (len(pending), now)
            self._sleep(self.poll_seconds)
        return pending

    def import_errors(self, job: Job) -> list[tuple[str, str]]:
        """(identifier, message) for each failed record of a bulk import job."""
        out = []
        try:
            for page in self.client.paginate(f"/{job.kind}/{job.id}/import-errors/"):
                for err in page["data"]:
                    a = err["attributes"]
                    who = (a.get("original_payload") or {}).get("email") or ""
                    out.append((who, f"{a.get('title', '')}: {a.get('detail', '')}".strip(": ")))
        except ApiError as exc:
            out.append(("", f"could not read errors for job {job.id}: {exc}"))
        return out
