import csv
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.config import Secret
from migtool.klaviyo import imports
from migtool.klaviyo.client import KlaviyoClient
from migtool.klaviyo.writes import Job, Writer, ca_properties, parse_value, profile_attributes
from migtool.output import new_run
from migtool.runlog import RunLog

API = "https://a.klaviyo.com/api"


@pytest.mark.parametrize("text,value", [
    ("true", True), ("3", 3), ("2.5", 2.5), ("01234", "01234"), ("1e3", "1e3"),
    ('["a",1]', ["a", 1]), ('{"k":"v"}', {"k": "v"}), ("[not json", "[not json"), ("hello", "hello"),
])
def test_parse_value(text, value):
    assert parse_value(text) == value


def test_profile_attributes_never_sends_ids_or_internal_properties():
    row = {"id": "CAPROF", "email": "a@example.com", "external_id": "CA-1", "anonymous_id": "anon",
           "first_name": "A", "last_name": "", "location.city": "Toronto", "location.latitude": "43.6",
           "properties.Language": "fr", "properties.$consent": '["email"]', "properties.empty": ""}
    attrs = profile_attributes(row, ca_properties(row, "RUN1"))
    assert attrs["email"] == "a@example.com" and attrs["first_name"] == "A"
    assert "last_name" not in attrs  # blank cells never clear destination values
    assert not {"id", "external_id", "anonymous_id"} & set(attrs)
    assert attrs["location"] == {"city": "Toronto", "latitude": 43.6}
    props = attrs["properties"]
    assert props["Language"] == "fr" and "$consent" not in props and "empty" not in props
    assert props["ca_external_id"] == "CA-1"
    assert (props["migrated_from"], props["migration_run_id"]) == ("ca", "RUN1")


def test_plan_profiles_import():
    rows = [
        {"email": "sub", "consent": "SUBSCRIBED", "suppression_reason": ""},
        {"email": "unsub", "consent": "UNSUBSCRIBED", "suppression_reason": "UNSUBSCRIBE"},
        {"email": "sub-bounced", "consent": "SUBSCRIBED", "suppression_reason": "HARD_BOUNCE"},
        {"email": "never", "consent": "NEVER_SUBSCRIBED", "suppression_reason": ""},
        {"email": "never-spam", "consent": "NEVER_SUBSCRIBED", "suppression_reason": "SPAM_COMPLAINT"},
    ]
    plan = imports.plan_profiles_import(rows)
    assert [r["email"] for r in plan["subscribe"]] == ["sub"]
    assert plan["unsubscribe"] == ["unsub"]
    assert plan["suppress"] == ["sub-bounced", "never-spam"]


def test_usable_skips_rows_with_reasons():
    batch = imports.usable([
        {"email": "A@Example.com"}, {"email": "", "phone_number": "+14165550100"},
        {"email": "a@example.com"}, {"email": "not-an-email"},
    ])
    assert [r["email"] for r in batch.rows] == ["a@example.com"]
    assert [reason for _, reason in batch.skipped] == [
        "no email (phone +14165550100)", "duplicate email in file", "not a valid email"]


class FakeWriter:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.calls = []

    def existing_emails(self, emails):
        return {e for e in emails if e in self.existing}

    def import_profiles(self, profiles, *, list_id):
        self.calls.append(("import", [p["email"] for p in profiles], list_id,
                           [sorted(p.get("properties", {})) for p in profiles]))
        return Job("profile-bulk-import-jobs", f"j{len(self.calls)}", len(profiles), {"status": "complete"})

    def subscribe(self, rows, *, list_id):
        self.calls.append(("subscribe", list(rows), list_id))

    def unsubscribe(self, emails):
        self.calls.append(("unsubscribe", list(emails)))

    def suppress(self, emails):
        self.calls.append(("suppress", list(emails)))
        return Job("profile-suppression-bulk-create-jobs", "s1", len(emails), {"status": "complete", "completed_count": len(emails)})

    def wait(self, jobs, *, timeout, progress):
        return []

    def import_errors(self, job):
        return []


def importer(tmp_path, writer, main_step):
    log = RunLog(new_run("klaviyo_sandbox", "t", base=tmp_path), echo=lambda _: None)
    return imports.Importer(writer, log, echo=lambda _: None, save_job=lambda j: None, main_step=main_step), log


def test_lists_add_tags_only_created_profiles_and_subscribes_with_consent_columns(tmp_path):
    w = FakeWriter(existing={"old@example.com"})
    imp, log = importer(tmp_path, w, "add")
    rows = [
        {"email": "old@example.com", "consent": "SUBSCRIBED", "consent_timestamp": "2020-01-01T00:00:00Z", "suppression_reason": ""},
        {"email": "new@example.com", "consent": "NEVER_SUBSCRIBED", "consent_timestamp": "", "suppression_reason": ""},
    ]
    created = imports.lists_add(imp, rows, ["email", "consent", "consent_timestamp"], list_id="L1", run_id="R")
    assert created == 1
    create, add, subscribe = w.calls
    assert create[:3] == ("import", ["new@example.com"], "L1") and "migrated_from" in create[3][0]
    assert add[:3] == ("import", ["old@example.com"], "L1") and add[3] == [[]]
    assert subscribe == ("subscribe", [("old@example.com", "2020-01-01T00:00:00Z")], "L1")
    assert log.counts["written"] == 2


def test_lists_add_without_consent_columns_never_subscribes(tmp_path):
    w = FakeWriter()
    imp, _ = importer(tmp_path, w, "add")
    imports.lists_add(imp, [{"email": "a@example.com", "consent": "SUBSCRIBED"}], ["email"], list_id="L1", run_id="R")
    assert [c[0] for c in w.calls] == ["import"]


@pytest.mark.parametrize("as_unsubscribe,expected", [(False, "suppress"), (True, "unsubscribe")])
def test_suppressions_import_creates_missing_then_suppresses(tmp_path, as_unsubscribe, expected):
    w = FakeWriter(existing={"old@example.com"})
    imp, log = importer(tmp_path, w, "suppress")
    rows = [{"email": "old@example.com"}, {"email": "new@example.com"}]
    assert imports.suppressions_import(imp, rows, run_id="R", as_unsubscribe=as_unsubscribe) == 1
    assert w.calls[0][:3] == ("import", ["new@example.com"], None)
    assert w.calls[1] == (expected, ["old@example.com", "new@example.com"])
    assert log.counts["written"] == 2


def test_profiles_import_subscribed_without_timestamp_is_an_error(tmp_path):
    w = FakeWriter()
    imp, log = importer(tmp_path, w, "import")
    imports.profiles_import(imp, [{"email": "a@example.com", "consent": "SUBSCRIBED", "consent_timestamp": ""}],
                            list_id="L1", run_id="R")
    assert log.counts["failed"] == 1
    assert not [c for c in w.calls if c[0] == "subscribe" and c[1]]


@respx.mock
def test_wait_returns_unfinished_jobs_after_timeout():
    respx.get(f"{API}/profile-suppression-bulk-create-jobs/s1/").mock(
        return_value=httpx.Response(200, json={"data": {"attributes": {"status": "processing"}}}))
    now = [0.0]
    w = Writer(KlaviyoClient(Secret("pk_x")), sleep=lambda s: now.__setitem__(0, now[0] + s),
               clock=lambda: now[0], poll_seconds=10)
    job = Job("profile-suppression-bulk-create-jobs", "s1", 1)
    assert w.wait([job], timeout=30) == [job]


@respx.mock
def test_existing_emails_uses_any_filter():
    route = respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json={
        "data": [{"attributes": {"email": "A@example.com"}}], "links": {"next": None}}))
    w = Writer(KlaviyoClient(Secret("pk_x")))
    assert w.existing_emails(["a@example.com", "b@example.com"]) == {"a@example.com"}
    assert route.calls.last.request.url.params["filter"] == 'any(email,["a@example.com","b@example.com"])'


def test_import_to_source_instance_is_refused_without_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_CA_API_KEY", "pk_x")
    f = tmp_path / "s.csv"
    f.write_text("email\na@example.com\n")
    with respx.mock:
        respx.get(f"{API}/accounts/").mock(return_value=httpx.Response(200, json={"data": [
            {"id": "Ka6Lvr", "attributes": {"contact_information": {"organization_name": "LOF CA"}}}]}))
        posts = respx.post(url__startswith=API).mock(return_value=httpx.Response(202))
        result = CliRunner().invoke(app, ["klaviyo", "suppressions", "import", "--to", "klaviyo_ca",
                                          "--file", str(f), "--yes"])
    assert result.exit_code != 0
    assert posts.call_count == 0


def test_suppress_submits_without_waiting(tmp_path):
    class NoWait(FakeWriter):
        def wait(self, jobs, *, timeout, progress):
            raise AssertionError("suppression jobs must not be waited on")
    w = NoWait()
    imp, log = importer(tmp_path, w, "suppress")
    imp.suppress([f"p{i}@example.com" for i in range(150)])
    assert [len(c[1]) for c in w.calls] == [100, 50]
    assert log.counts == {"read": 0, "written": 150, "skipped": 0, "failed": 0}


@respx.mock
def test_suppressions_check_reports_states(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")

    def prof(email, reasons):
        return {"attributes": {"email": email, "subscriptions": {"email": {"marketing": {
            "suppression": [{"reason": r, "timestamp": "2026-09-24T00:00:00+00:00"} for r in reasons]}}}}}

    respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json={"links": {"next": None}, "data": [
        prof("sup@example.com", ["UNSUBSCRIBE", "USER_SUPPRESSED"]), prof("unsub@example.com", ["UNSUBSCRIBE"]),
        prof("clear@example.com", []),
    ]}))
    f = tmp_path / "s.csv"
    f.write_text("email\nsup@example.com\nunsub@example.com\nclear@example.com\nmissing@example.com\n")
    result = CliRunner().invoke(app, ["klaviyo", "suppressions", "check", "--instance", "klaviyo_sandbox", "--file", str(f)])
    assert result.exit_code == 0, result.output
    assert "suppressed      1" in result.output and "no profile      1" in result.output
    [out] = (tmp_path / "exports/klaviyo_sandbox/suppressions-check").glob("*.not_suppressed.csv")
    with open(out, newline="") as fh:
        states = {r["email"]: r["state"] for r in csv.DictReader(fh)}
    assert states == {"unsub@example.com": "unsubscribed", "clear@example.com": "not suppressed",
                      "missing@example.com": "no profile"}
