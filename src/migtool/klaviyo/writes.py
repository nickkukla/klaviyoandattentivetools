"""Klaviyo writes: CSV rows to API payloads, batched bulk jobs, job tracking.

Shared by `profiles import`, `suppressions import` and `lists add`. The CSV
layout is the one `profiles export` writes; a file with only an `email`
column works too.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from migtool.http import ApiError
from migtool.klaviyo.client import KlaviyoClient

IMPORT_BATCH = 1000  # profiles per bulk import job (limit 10,000 and 5 MB)
IMPORT_MAX_BYTES = 4_500_000  # stay under Klaviyo's 5 MB request limit
PROFILE_MAX_BYTES = 100_000  # Klaviyo's limit for one profile in a bulk import
SUBSCRIBE_BATCH = 1000  # limit 1,000
UNSUBSCRIBE_BATCH = 100
SUPPRESS_BATCH = 100  # limit 100
LOOKUP_BATCH = 100  # emails per `any(email,[…])` lookup
# Klaviyo validates a bulk request as a whole, so one bad row refuses the
# batch. Its error points at the row (`/data/attributes/profiles/data/<i>/…`),
# which is dropped and the rest resent. An error pointing anywhere else (a bad
# list ID: `/data`, `/data/relationships/…`) is about the whole request, so the
# run stops instead. 413 (too large) has no row to point at and is halved.
REJECTED_STATUSES = {400, 409, 422}
ROW_POINTER = re.compile(r"^/data/attributes/profiles/data/(\d+)(/|$)")

# Fields copied onto the destination profile. The source `id`, `external_id`
# and `anonymous_id` are never sent.
FIELDS = ["email", "phone_number", "first_name", "last_name", "organization", "title", "locale", "image"]
NUMERIC_LOCATION = {"latitude", "longitude"}
CONSENT_COLUMNS = {"consent", "consent_timestamp"}
MIGRATED_FROM = "ca"
CUSTOM_SOURCE = "migtool CA migration"


PROPERTY_TYPES = ("number", "bool", "json", "text")
INTEGER = re.compile(r"[+-]?\d+")


def property_column(column: str) -> tuple[str, str]:
    """`properties.orders#number` → (`orders`, `number`). Columns without a
    `#type` suffix are text; `#text` marks a text property whose own name ends
    in a type suffix (`code#number#text` → `code#number`)."""
    name = column.removeprefix("properties.")
    key, sep, kind = name.rpartition("#")
    if sep and kind in PROPERTY_TYPES:
        return key, kind
    return name, "text"


def finite_number(text: str) -> int | float:
    """Whole numbers exactly (no float rounding); others as floats. NaN and
    infinity are refused: they can't be sent as JSON."""
    text = text.strip()
    if INTEGER.fullmatch(text):
        return int(text)
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"'{text}' is not a finite number")
    return number


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def typed_value(text: str, kind: str) -> Any:
    """A property cell as the type its column says. Text is never guessed at,
    so `"123"` in a text column stays a string."""
    if kind == "number":
        return finite_number(text)
    if kind == "bool":
        if text.strip().lower() not in ("true", "false"):
            raise ValueError(f"'{text}' is not true or false")
        return text.strip().lower() == "true"
    if kind == "json":
        value = json.loads(text, parse_constant=_no_constant)
        json.dumps(value, allow_nan=False)  # refuses 1e999 → inf anywhere inside
        return value
    return text


def profile_attributes(row: dict[str, str], extra_properties: dict[str, Any]) -> dict[str, Any]:
    """Profile attributes for a bulk import. Blank cells are left out, so they
    never clear a value in the destination."""
    attrs: dict[str, Any] = {k: row[k] for k in FIELDS if row.get(k)}
    location: dict[str, Any] = {}
    for column, text in row.items():
        if not column.startswith("location.") or not text:
            continue
        key = column.removeprefix("location.")
        try:
            location[key] = float(finite_number(text)) if key in NUMERIC_LOCATION else text
        except ValueError as exc:
            raise ValueError(f"{column}: {exc}") from exc
    if location:
        attrs["location"] = location
    props: dict[str, Any] = {}
    for column, text in row.items():
        # `$…` keys are Klaviyo-internal (e.g. `$consent`) and must not be written.
        if not column.startswith("properties.") or not text or column.startswith("properties.$"):
            continue
        key, kind = property_column(column)
        try:
            props[key] = typed_value(text, kind)
        except ValueError as exc:
            raise ValueError(f"{column}: {exc}") from exc
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


def identity(profile: dict[str, Any]) -> str:
    """How a profile or row is identified: its email, or its phone number when
    it has no email (phone-only profiles)."""
    return (profile.get("email") or "").lower() or (profile.get("phone_number") or "")


def batches(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def byte_batches(items: Sequence[Any], *, count: int, max_bytes: int) -> Iterator[list[Any]]:
    """Batches of at most `count` items and about `max_bytes` of JSON."""
    batch: list[Any] = []
    total = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False).encode()) + 40  # + JSON:API wrapper
        if batch and (len(batch) >= count or total + size > max_bytes):
            yield batch
            batch, total = [], 0
        batch.append(item)
        total += size
    if batch:
        yield batch


def row_errors(exc: ApiError) -> dict[int, str] | None:
    """{row index: reason} when every error in Klaviyo's response points at a
    profile in the request; None when any error is about the request itself."""
    try:
        errors = json.loads(exc.body)["errors"]
    except (ValueError, KeyError, TypeError):
        return None
    rows: dict[int, str] = {}
    for err in errors:
        match = ROW_POINTER.match(((err.get("source") or {}).get("pointer")) or "")
        if not match:
            return None
        rows[int(match.group(1))] = f"{exc.status} {err.get('title', '')}: {err.get('detail', '')}".strip(": ")
    return rows


def api_error_detail(exc: ApiError) -> str:
    """The first error detail from a Klaviyo error body, or the raw text."""
    try:
        err = json.loads(exc.body)["errors"][0]
        return f"{exc.status} {err.get('title', '')}: {err.get('detail', '')}".strip(": ")
    except (ValueError, KeyError, IndexError, TypeError):
        return f"{exc.status}: {exc.detail[:200]}"


Refused = Callable[[str, str], None]  # (identifier, reason), reported as it happens


def _ignore(who: str, reason: str) -> None:
    pass


@dataclass
class Job:
    kind: str  # API collection, e.g. "profile-bulk-import-jobs"
    id: str
    size: int
    attributes: dict[str, Any] = field(default_factory=dict)
    emails: list[str] = field(default_factory=list)

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

    def isolate(
        self,
        items: list[Any],
        send: Callable[[list[Any]], Any],
        who: Callable[[Any], str],
        on_sent: Callable[[list[Any], Any], None] = lambda chunk, result: None,
        on_refused: Refused = _ignore,
    ) -> list[tuple[str, str]]:
        """Send `items` with `send`, calling `on_sent(chunk, result)` as soon as
        each request is accepted (so nothing accepted is lost if a later one
        fails). Rows Klaviyo points at as invalid are dropped, reported to
        `on_refused` at once, and the rest resent. Returns (identifier, reason)
        for each refused row. Raises for errors about the whole request, and for
        anything else unexpected."""
        rejected: list[tuple[str, str]] = []
        pending = list(items)
        while pending:
            try:
                result = send(pending)
            except ApiError as exc:
                if exc.status == 413 and len(pending) > 1:
                    mid = len(pending) // 2
                    return (rejected + self.isolate(pending[:mid], send, who, on_sent, on_refused)
                            + self.isolate(pending[mid:], send, who, on_sent, on_refused))
                bad = row_errors(exc) if exc.status in REJECTED_STATUSES else None
                if bad is None or not bad or max(bad) >= len(pending):
                    raise
                for i in sorted(bad, reverse=True):
                    rejected.append((who(pending[i]), bad[i]))
                    on_refused(who(pending[i]), bad[i])
                    del pending[i]
                continue
            on_sent(pending, result)
            break
        return rejected

    def import_profiles(
        self, profiles: Sequence[dict[str, Any]], *, list_id: str | None, on_job: Callable[[Job], None],
        on_refused: Refused = _ignore,
    ) -> list[tuple[str, str]]:
        """Submit bulk import jobs, batched by count and size, passing each job
        to `on_job` as soon as Klaviyo accepts it. Returns refused rows,
        including any profile over Klaviyo's per-profile size limit."""
        rejected: list[tuple[str, str]] = []
        fits = []
        for p in profiles:
            if len(json.dumps(p, ensure_ascii=False).encode()) > PROFILE_MAX_BYTES:
                reason = f"profile is larger than Klaviyo's {PROFILE_MAX_BYTES // 1000} KB limit"
                rejected.append((identity(p), reason))
                on_refused(identity(p), reason)
            else:
                fits.append(p)
        for chunk in byte_batches(fits, count=IMPORT_BATCH, max_bytes=IMPORT_MAX_BYTES):
            rejected += self.isolate(
                chunk, lambda c: self._import_job(c, list_id), identity,
                lambda c, job: on_job(job), on_refused,
            )
        return rejected

    def _import_job(self, profiles: Sequence[dict[str, Any]], list_id: str | None) -> Job:
        body: dict[str, Any] = {"data": {
            "type": "profile-bulk-import-job",
            "attributes": {"profiles": {"data": [{"type": "profile", "attributes": p} for p in profiles]}},
        }}
        if list_id:
            body["data"]["relationships"] = {"lists": {"data": [{"type": "list", "id": list_id}]}}
        data = self.client.post("/profile-bulk-import-jobs/", body, tier="M")["data"]
        return Job("profile-bulk-import-jobs", data["id"], len(profiles), data["attributes"],
                   [identity(p) for p in profiles])

    def import_result(self, job: Job) -> tuple[set[str], list[tuple[str, str]], set[str]]:
        """(succeeded, (identity, reason) failures, unknown outcome) for a
        finished import job, by identity (email, or phone for phone-only rows).
        Counts come from the job itself; anything that can't be attributed is
        unknown, never assumed successful."""
        emails = {e for e in job.emails if e}
        if job.status != "complete":
            return set(), [], emails
        if not (job.attributes.get("failed_count") or 0):
            return emails, [], set()
        try:
            failures = []
            for page in self.client.paginate(f"/{job.kind}/{job.id}/import-errors/"):
                for err in page["data"]:
                    a = err["attributes"]
                    who = identity(a.get("original_payload") or {})
                    failures.append((who, f"{a.get('title', '')}: {a.get('detail', '')}".strip(": ")))
        except ApiError:
            return set(), [], emails
        failed = {e for e, _ in failures if e}
        if len(failed) < (job.attributes.get("failed_count") or 0):
            # Some failures can't be tied to a profile: the rest are unknown.
            return set(), [f for f in failures if f[0]], emails - failed
        return emails - failed, failures, set()

    def subscribe(
        self, rows: Sequence[tuple[str, str]], *, list_id: str, on_sent: Callable[[int], None],
        on_refused: Refused = _ignore,
    ) -> list[tuple[str, str]]:
        """Historical-import subscribe: (email, consented_at) pairs, no job to
        track. `on_sent(n)` is called as each request is accepted. Returns refused rows."""
        rejected: list[tuple[str, str]] = []
        for chunk in batches(list(rows), SUBSCRIBE_BATCH):
            rejected += self.isolate(list(chunk), lambda c: self._subscribe(c, list_id), lambda r: r[0],
                                     lambda c, _: on_sent(len(c)), on_refused)
        return rejected

    def _subscribe(self, rows: Sequence[tuple[str, str]], list_id: str) -> None:
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

    def unsubscribe(
        self, emails: Sequence[str], *, on_sent: Callable[[int], None], on_refused: Refused = _ignore
    ) -> list[tuple[str, str]]:
        """Unsubscribe from email marketing, no job to track. `on_sent(n)` is
        called as each request is accepted. Returns refused rows."""
        rejected: list[tuple[str, str]] = []
        for chunk in batches(list(emails), UNSUBSCRIBE_BATCH):
            rejected += self.isolate(list(chunk), self._unsubscribe, lambda e: e, lambda c, _: on_sent(len(c)),
                                     on_refused)
        return rejected

    def _unsubscribe(self, emails: Sequence[str]) -> None:
        self.client.post("/profile-subscription-bulk-delete-jobs/", {"data": {
            "type": "profile-subscription-bulk-delete-job",
            "attributes": {"profiles": {"data": [
                {"type": "profile", "attributes": {"email": e, "subscriptions": {
                    "email": {"marketing": {"consent": "UNSUBSCRIBED"}}}}}
                for e in emails
            ]}},
        }}, tier="L")

    def suppress(
        self, emails: Sequence[str], *, on_job: Callable[[Job], None], on_refused: Refused = _ignore
    ) -> list[tuple[str, str]]:
        """Submit suppression jobs, passing each to `on_job` as soon as it's
        accepted. Returns refused rows."""
        rejected: list[tuple[str, str]] = []
        for chunk in batches(list(emails), SUPPRESS_BATCH):
            rejected += self.isolate(list(chunk), self._suppress, lambda e: e, lambda c, job: on_job(job),
                                     on_refused)
        return rejected

    def _suppress(self, emails: Sequence[str]) -> Job:
        data = self.client.post("/profile-suppression-bulk-create-jobs/", {"data": {
            "type": "profile-suppression-bulk-create-job",
            "attributes": {"profiles": {"data": [
                {"type": "profile", "attributes": {"email": e}} for e in emails
            ]}},
        }}, tier="L")["data"]
        return Job("profile-suppression-bulk-create-jobs", data["id"], len(emails), data["attributes"], list(emails))

    def create_list(self, name: str) -> str:
        """Create an empty list and return its ID."""
        return self.client.post("/lists/", {"data": {"type": "list", "attributes": {"name": name}}},
                                tier="M")["data"]["id"]

    def existing_emails(self, emails: Iterable[str]) -> set[str]:
        """Which of `emails` already have a profile (compared lowercased)."""
        found: set[str] = set()
        for chunk in batches(sorted({e.lower() for e in emails}), LOOKUP_BATCH):
            listed = ",".join(json.dumps(e) for e in chunk)
            params = {"filter": f"any(email,[{listed}])", "fields[profile]": "email", "page[size]": "100"}
            for page in self.client.paginate("/profiles/", tier="L", params=params):
                found.update((p["attributes"].get("email") or "").lower() for p in page["data"])
        return found

    def existing_phones(self, phones: Iterable[str]) -> set[str]:
        """Which of `phones` (E.164) already have a profile."""
        found: set[str] = set()
        for chunk in batches(sorted(set(phones)), LOOKUP_BATCH):
            listed = ",".join(json.dumps(p) for p in chunk)
            params = {"filter": f"any(phone_number,[{listed}])", "fields[profile]": "phone_number", "page[size]": "100"}
            for page in self.client.paginate("/profiles/", tier="L", params=params):
                found.update(p["attributes"].get("phone_number") or "" for p in page["data"])
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
