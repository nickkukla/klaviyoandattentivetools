import csv
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.config import Secret
from migtool.http import ApiError
from migtool.klaviyo import imports
from migtool.klaviyo.client import KlaviyoClient
from migtool.klaviyo.writes import (
    identity,
    Job,
    Writer,
    byte_batches,
    ca_properties,
    profile_attributes,
    property_column,
    typed_value,
)
from migtool.output import ResumableExport, new_run
from migtool.runlog import RunLog
from migtool.state import StateStore

API = "https://a.klaviyo.com/api"


# --- Reading cells -----------------------------------------------------------

@pytest.mark.parametrize("column,expected", [
    ("properties.orders#number", ("orders", "number")), ("properties.vip#bool", ("vip", "bool")),
    ("properties.tags#json", ("tags", "json")), ("properties.zip", ("zip", "text")),
    ("properties.a#b", ("a#b", "text")), ("properties.code#number#text", ("code#number", "text")),
])
def test_property_column(column, expected):
    assert property_column(column) == expected


@pytest.mark.parametrize("text,kind,value", [
    ("123", "text", "123"), ("true", "text", "true"), ("[1,2]", "text", "[1,2]"),
    ("3", "number", 3), ("2.5", "number", 2.5), ("1e3", "number", 1000.0),
    ("9007199254740993", "number", 9007199254740993), ("-7", "number", -7),
    ("TRUE", "bool", True), ('["a",1]', "json", ["a", 1]), ('"x"', "json", "x"),
])
def test_typed_value_never_guesses_text(text, kind, value):
    assert typed_value(text, kind) == value
    assert type(typed_value(text, kind)) is type(value)


def test_profile_attributes_never_sends_ids_or_internal_properties():
    row = {"id": "CAPROF", "email": "a@example.com", "external_id": "CA-1", "anonymous_id": "anon",
           "first_name": "A", "last_name": "", "location.city": "Toronto", "location.latitude": "43.6",
           "properties.Language": "fr", "properties.code": "123", "properties.orders#number": "3",
           "properties.$consent": '["email"]', "properties.empty": ""}
    attrs = profile_attributes(row, ca_properties(row, "RUN1"))
    assert attrs["email"] == "a@example.com" and attrs["first_name"] == "A"
    assert "last_name" not in attrs  # blank cells never clear destination values
    assert not {"id", "external_id", "anonymous_id"} & set(attrs)
    assert attrs["location"] == {"city": "Toronto", "latitude": 43.6}
    props = attrs["properties"]
    assert props["Language"] == "fr" and props["code"] == "123" and props["orders"] == 3
    assert "$consent" not in props and "empty" not in props
    assert props["ca_external_id"] == "CA-1"
    assert (props["migrated_from"], props["migration_run_id"]) == ("ca", "RUN1")


@pytest.mark.parametrize("column,text", [
    ("properties.x#number", "nan"), ("properties.x#number", "inf"), ("properties.x#number", "1e999"),
    ("location.latitude", "NaN"),
])
def test_non_finite_numbers_are_refused(column, text):
    with pytest.raises(ValueError, match=column):
        profile_attributes({"email": "a@example.com", column: text}, {})


@pytest.mark.parametrize("text", ['{"score":NaN}', "[1e999]", '{"a":{"b":[Infinity]}}', "-Infinity"])
def test_json_cells_must_be_strict_json(text):
    with pytest.raises(ValueError, match="properties.x#json"):
        profile_attributes({"email": "a@example.com", "properties.x#json": text}, {})


def test_bad_typed_cell_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="properties.orders#number"):
        profile_attributes({"email": "a@example.com", "properties.orders#number": "three"}, {})


def test_read_rows_accepts_excel_bom(tmp_path):
    f = tmp_path / "excel.csv"
    f.write_bytes("﻿email,first_name\r\na@example.com,Ann\r\n".encode("utf-8"))
    columns, rows = imports.read_rows(f)
    assert columns == ["email", "first_name"] and rows == [{"email": "a@example.com", "first_name": "Ann"}]


def test_typed_export_round_trips_through_import(tmp_path):
    exp = ResumableExport("klaviyo_ca", "profiles", ["id", "email"], staged=True, typed_prefix="properties.",
                          store=StateStore(tmp_path / "state"), base=tmp_path / "exports", echo=lambda _: None)
    sent = {"properties.code": "123", "properties.orders": 3, "properties.vip": True,
            "properties.tags": ["a", "b"], "properties.mixed": "x"}
    exp.write({"id": "1", "email": "a@example.com", **sent})
    exp.write({"id": "2", "email": "b@example.com", "properties.mixed": 7, "properties.orders": 2.5})
    exp.checkpoint(None)
    exp.finish()
    _, rows = imports.read_rows(exp.run.path(".csv"))
    assert set(rows[0]) >= {"properties.code", "properties.orders#number", "properties.vip#bool",
                            "properties.tags#json", "properties.mixed#json"}
    props = profile_attributes(rows[0], {})["properties"]
    assert props == {"code": "123", "orders": 3, "vip": True, "tags": ["a", "b"], "mixed": "x"}
    assert profile_attributes(rows[1], {})["properties"] == {"mixed": 7, "orders": 2.5}


def test_property_named_like_a_type_suffix_does_not_collide(tmp_path):
    exp = ResumableExport("klaviyo_ca", "profiles", ["id", "email"], staged=True, typed_prefix="properties.",
                          store=StateStore(tmp_path / "state"), base=tmp_path / "exports", echo=lambda _: None)
    exp.write({"id": "1", "email": "a@example.com", "properties.code": 5, "properties.code#number": "text value",
               "properties.big": 9007199254740993})
    exp.checkpoint(None)
    exp.finish()
    columns, rows = imports.read_rows(exp.run.path(".csv"))
    assert {"properties.code#number", "properties.code#number#text"} <= set(columns)
    assert profile_attributes(rows[0], {})["properties"] == {
        "code": 5, "code#number": "text value", "big": 9007199254740993}


# --- Deciding consent --------------------------------------------------------

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


def test_older_hard_bounce_behind_newer_unsubscribe_is_still_suppressed():
    row = {"email": "a", "consent": "UNSUBSCRIBED", "suppression_reason": "UNSUBSCRIBE",
           "suppressions": json.dumps([{"reason": "HARD_BOUNCE", "timestamp": "2024-01-01T00:00:00+00:00"},
                                       {"reason": "UNSUBSCRIBE", "timestamp": "2025-01-01T00:00:00+00:00"}])}
    plan = imports.plan_profiles_import([row])
    assert plan["unsubscribe"] == ["a"] and plan["suppress"] == ["a"]


def test_usable_skips_rows_with_reasons():
    batch = imports.usable([
        {"email": "A@Example.com"}, {"email": "", "phone_number": "+14165550100"},
        {"email": "a@example.com"}, {"email": "not-an-email"},
    ])
    assert [r["email"] for r in batch.rows] == ["a@example.com"]
    assert [reason for _, reason in batch.skipped] == [
        "no email (phone +14165550100)", "duplicate email in file", "not a valid email"]


# --- The real write client against mocked Klaviyo HTTP ----------------------

def writer():
    now = [0.0]
    return Writer(KlaviyoClient(Secret("pk_x")), sleep=lambda s: now.__setitem__(0, now[0] + s),
                  clock=lambda: now[0], poll_seconds=1)


def emails_in(request) -> list[str]:
    return [p["attributes"]["email"] for p in json.loads(request.content)["data"]["attributes"]["profiles"]["data"]]


def job_body(jid, status="queued", **counts):
    return {"data": {"id": jid, "attributes": {"status": status, **counts}}}


def row_error(index, detail="bad phone"):
    return {"errors": [{"status": 400, "title": "Invalid input.", "detail": detail,
                        "source": {"pointer": f"/data/attributes/profiles/data/{index}/attributes/phone_number"}}]}


@respx.mock
def test_row_errors_drop_only_the_pointed_rows_and_resend_the_rest():
    def respond(request):
        emails = emails_in(request)
        for i, e in enumerate(emails):
            if e.startswith("bad"):
                return httpx.Response(400, json=row_error(i))
        return httpx.Response(202, json=job_body(f"j{len(emails)}"))
    route = respx.post(f"{API}/profile-bulk-import-jobs/").mock(side_effect=respond)
    profiles = [{"email": e} for e in ("a@example.com", "bad1@example.com", "b@example.com", "bad2@example.com")]
    jobs = []
    rejected = writer().import_profiles(profiles, list_id="L1", on_job=jobs.append)
    assert rejected == [("bad1@example.com", "400 Invalid input.: bad phone"),
                        ("bad2@example.com", "400 Invalid input.: bad phone")]
    assert [j.emails for j in jobs] == [["a@example.com", "b@example.com"]]
    assert route.call_count == 3  # not one request per row
    first = json.loads(route.calls[0].request.content)["data"]
    assert first["relationships"] == {"lists": {"data": [{"type": "list", "id": "L1"}]}}


@respx.mock
def test_long_error_response_still_isolates_rows():
    detail = "The phone number provided either does not exist or is ineligible to receive SMS. " * 2
    def respond(request):
        emails = emails_in(request)
        bad = [i for i, e in enumerate(emails) if e.startswith("bad")]
        if bad:
            return httpx.Response(400, json={"errors": [
                {"status": 400, "title": "Invalid input.", "detail": detail,
                 "source": {"pointer": f"/data/attributes/profiles/data/{i}/attributes/phone_number"}} for i in bad]})
        return httpx.Response(202, json=job_body("OK"))
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(side_effect=respond)
    profiles = [{"email": f"bad{i}@example.com"} for i in range(4)] + [{"email": "good@example.com"}]
    jobs = []
    rejected = writer().import_profiles(profiles, list_id=None, on_job=jobs.append)
    assert len(rejected) == 4 and [j.emails for j in jobs] == [["good@example.com"]]


def test_api_error_keeps_full_body_but_short_message():
    body = "x" * 2000
    exc = ApiError("POST", "https://a.klaviyo.com/api/x/", 400, body)
    assert exc.body == body and len(exc.detail) == 500 and len(str(exc)) < 700


@respx.mock
def test_refused_row_is_logged_even_if_the_next_request_fails(tmp_path):
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(side_effect=[
        httpx.Response(400, json=row_error(0, "Invalid email address")), httpx.Response(401, text="revoked")])
    log = RunLog(new_run("klaviyo_sandbox", "t", base=tmp_path), echo=lambda _: None)
    imp = imports.Importer(writer(), log, echo=lambda _: None, save_job=lambda j: None, main_step="import")
    with pytest.raises(ApiError):
        imp.import_profiles([{"email": "bad@example.com"}, {"email": "ok@example.com"}], list_id=None, stage="import")
    log.finish()
    assert "bad@example.com" in log.errors_path.read_text()
    assert "Invalid email address" in log.errors_path.read_text()


@pytest.mark.parametrize("pointer", ["/data", "/data/relationships/lists/data/0/id", None])
@respx.mock
def test_request_wide_error_stops_without_splitting(pointer):
    body = {"errors": [{"status": 400, "title": "Invalid input.", "detail": "List ID X does not exist.",
                        **({"source": {"pointer": pointer}} if pointer else {})}]}
    route = respx.post(f"{API}/profile-bulk-import-jobs/").mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(ApiError, match="does not exist"):
        writer().import_profiles([{"email": f"p{i}@example.com"} for i in range(8)], list_id="X", on_job=lambda j: None)
    assert route.call_count == 1


@respx.mock
def test_too_large_request_is_halved():
    def respond(request):
        n = len(emails_in(request))
        return httpx.Response(413, text="too large") if n > 2 else httpx.Response(202, json=job_body(f"j{n}"))
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(side_effect=respond)
    jobs = []
    assert writer().import_profiles([{"email": f"p{i}@example.com"} for i in range(8)], list_id=None,
                                    on_job=jobs.append) == []
    assert sorted(len(j.emails) for j in jobs) == [2, 2, 2, 2]


@respx.mock
def test_server_error_stops_instead_of_splitting():
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(ApiError):
        writer().import_profiles([{"email": "a@example.com"}], list_id=None, on_job=lambda j: None)


def test_byte_batches_respect_size_and_count():
    items = [{"p": "x" * 1000} for _ in range(10)]
    by_size = list(byte_batches(items, count=100, max_bytes=3500))
    assert [len(b) for b in by_size] == [3, 3, 3, 1]
    assert [len(b) for b in byte_batches(items, count=4, max_bytes=10**9)] == [4, 4, 2]


@respx.mock
def test_profile_over_100kb_is_refused_before_sending():
    route = respx.post(f"{API}/profile-bulk-import-jobs/")
    rejected = writer().import_profiles([{"email": "big@example.com", "properties": {"x": "y" * 150_000}}],
                                        list_id=None, on_job=lambda j: None)
    assert route.call_count == 0
    assert rejected == [("big@example.com", "profile is larger than Klaviyo's 100 KB limit")]


@pytest.mark.parametrize("status,counts,errors,expected", [
    ("complete", {"failed_count": 0}, None, ({"a", "b"}, [], set())),
    ("complete", {"failed_count": 1}, [{"email": "b"}], ({"a"}, [("b", "Bad: phone")], set())),
    ("complete", {"failed_count": 1}, "unreadable", (set(), [], {"a", "b"})),
    ("complete", {"failed_count": 2}, [{"email": "b"}], (set(), [("b", "Bad: phone")], {"a"})),
    ("failed", {}, None, (set(), [], {"a", "b"})),
    ("processing", {}, None, (set(), [], {"a", "b"})),
])
@respx.mock
def test_import_result_never_assumes_success(status, counts, errors, expected):
    job = Job("profile-bulk-import-jobs", "J", 2, {"status": status, **counts}, ["a", "b"])
    route = respx.get(f"{API}/profile-bulk-import-jobs/J/import-errors/")
    if errors == "unreadable":
        route.mock(return_value=httpx.Response(404))
    elif errors is not None:
        route.mock(return_value=httpx.Response(200, json={"links": {"next": None}, "data": [
            {"attributes": {"title": "Bad", "detail": "phone", "original_payload": p}} for p in errors]}))
    assert writer().import_result(job) == expected


def run_import(tmp_path, monkeypatch, csv_text, extra_args=()):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    monkeypatch.setattr(imports, "IMPORT_WAIT", 0)
    f = tmp_path / "in.csv"
    f.write_text(csv_text)
    respx.get(f"{API}/accounts/").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "T2aEdf", "attributes": {"contact_information": {"organization_name": "Dev"}}}]}))
    result = CliRunner().invoke(app, ["klaviyo", "profiles", "import", "--to", "klaviyo_sandbox",
                                      "--file", str(f), "--list-id", "L1", "--yes", *extra_args])
    [manifest] = (tmp_path / "exports/klaviyo_sandbox/profiles-import").glob("manifest.json")
    return result, json.loads(manifest.read_text())["runs"][-1]


PROFILES_CSV = ("email,consent,consent_timestamp,suppression_reason\n"
                "ok@example.com,SUBSCRIBED,2022-01-01T00:00:00Z,\n"
                "fails@example.com,SUBSCRIBED,2022-01-01T00:00:00Z,\n")


@respx.mock
def test_consent_is_held_back_for_profiles_whose_import_failed(tmp_path, monkeypatch):
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(return_value=httpx.Response(202, json=job_body("J")))
    respx.get(f"{API}/profile-bulk-import-jobs/J/").mock(
        return_value=httpx.Response(200, json=job_body("J", "complete", completed_count=1, failed_count=1)))
    respx.get(f"{API}/profile-bulk-import-jobs/J/import-errors/").mock(return_value=httpx.Response(200, json={
        "links": {"next": None}, "data": [{"attributes": {"title": "Invalid", "detail": "phone",
                                                          "original_payload": {"email": "fails@example.com"}}}]}))
    subscribe = respx.post(f"{API}/profile-subscription-bulk-create-jobs/").mock(return_value=httpx.Response(202))
    result, run = run_import(tmp_path, monkeypatch, PROFILES_CSV)
    assert result.exit_code == 1
    sent = json.loads(subscribe.calls.last.request.content)["data"]["attributes"]
    assert [p["attributes"]["email"] for p in sent["profiles"]["data"]] == ["ok@example.com"]
    assert sent["historical_import"] is True
    assert run["counts"]["written"] == 1 and run["counts"]["failed"] == 1
    assert run["counts"]["steps"]["consent_held_back"] == 1


@respx.mock
def test_pending_import_holds_back_all_consent(tmp_path, monkeypatch):
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(return_value=httpx.Response(202, json=job_body("J")))
    respx.get(f"{API}/profile-bulk-import-jobs/J/").mock(return_value=httpx.Response(200, json=job_body("J", "processing")))
    subscribe = respx.post(f"{API}/profile-subscription-bulk-create-jobs/")
    result, run = run_import(tmp_path, monkeypatch, PROFILES_CSV)
    assert result.exit_code == 1 and subscribe.call_count == 0
    assert run["counts"]["written"] == 0 and run["counts"]["failed"] == 2


@respx.mock
def test_accepted_job_is_saved_when_a_later_batch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("migtool.klaviyo.writes.IMPORT_BATCH", 1)
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(side_effect=[
        httpx.Response(202, json=job_body("FIRST")), httpx.Response(401, text="revoked")])
    result, run = run_import(tmp_path, monkeypatch, PROFILES_CSV)
    assert result.exit_code == 1 and run["status"] == "aborted"
    saved = json.loads((tmp_path / "state/klaviyo_sandbox/jobs.json").read_text())
    assert [j["id"] for j in saved] == ["FIRST"]


@respx.mock
def test_unexpected_error_still_writes_manifest(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(Writer, "import_profiles", boom)
    result, run = run_import(tmp_path, monkeypatch, PROFILES_CSV)
    assert result.exit_code == 1 and run["status"] == "aborted"


@respx.mock
def test_suppress_job_saved_when_a_later_batch_fails(tmp_path):
    respx.post(f"{API}/profile-suppression-bulk-create-jobs/").mock(side_effect=[
        httpx.Response(202, json=job_body("S1")), httpx.Response(401, text="revoked")])
    w = writer()
    log = RunLog(new_run("klaviyo_sandbox", "t", base=tmp_path), echo=lambda _: None)
    saved = []
    imp = imports.Importer(w, log, echo=lambda _: None, save_job=saved.append, main_step="suppress")
    with pytest.raises(ApiError):
        imp.suppress([f"p{i}@example.com" for i in range(150)])
    assert [j.id for j in saved] == ["S1"] and log.counts["written"] == 100


@respx.mock
def test_aborted_run_still_writes_manifest_and_summary(tmp_path, monkeypatch):
    respx.post(f"{API}/profile-bulk-import-jobs/").mock(return_value=httpx.Response(401, text="revoked"))
    result, run = run_import(tmp_path, monkeypatch, PROFILES_CSV)
    assert result.exit_code == 1
    assert run["status"] == "aborted" and "migration_run_id" in run
    assert "read 2" in result.output
    [errors] = (tmp_path / "exports/klaviyo_sandbox/profiles-import").glob("*.errors.csv")
    assert "stopped" in errors.read_text()


# --- Importer steps with a fake writer ---------------------------------------

class FakeWriter:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.calls = []

    def existing_emails(self, emails):
        return {e for e in emails if e in self.existing}

    def existing_phones(self, phones):
        return {p for p in phones if p in self.existing}

    def import_profiles(self, profiles, *, list_id, on_job, on_refused=None):
        emails = [identity(p) for p in profiles]
        self.calls.append(("import", emails, list_id, [sorted(p.get("properties", {})) for p in profiles]))
        on_job(Job("profile-bulk-import-jobs", f"j{len(self.calls)}", len(profiles), {"status": "complete"}, emails))
        return []

    def import_result(self, job):
        return set(job.emails), [], set()

    def subscribe(self, rows, *, list_id, on_sent, on_refused=None):
        self.calls.append(("subscribe", list(rows), list_id))
        on_sent(len(rows))
        return []

    def unsubscribe(self, emails, *, on_sent, on_refused=None):
        self.calls.append(("unsubscribe", list(emails)))
        on_sent(len(emails))
        return []

    def suppress(self, emails, *, on_job, on_refused=None):
        self.calls.append(("suppress", list(emails)))
        on_job(Job("profile-suppression-bulk-create-jobs", "s1", len(emails), {"status": "queued"}, list(emails)))
        return []

    def wait(self, jobs, *, timeout, progress):
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


def test_lists_add_matches_phone_only_rows_by_phone(tmp_path):
    w = FakeWriter(existing={"a@example.com", "+14165550100"})
    imp, log = importer(tmp_path, w, "add")
    batch = imports.usable([{"email": "A@example.com", "phone_number": ""},
                            {"email": "", "phone_number": "+14165550100"},
                            {"email": "", "phone_number": "+14165550100"},
                            {"email": "", "phone_number": "4165550199"}], phones=True)
    assert [r["email"] or r["phone_number"] for r in batch.rows] == ["a@example.com", "+14165550100"]
    assert [reason for _, reason in batch.skipped] == ["duplicate phone number in file", "no email (phone 4165550199)"]
    assert imports.lists_add(imp, batch.rows, ["email", "phone_number"], list_id="L1", run_id="R") == 0
    [add] = w.calls
    assert add[:3] == ("import", ["a@example.com", "+14165550100"], "L1") and add[3] == [[], []]
    assert log.counts["written"] == 2


def test_phone_only_rows_are_skipped_unless_asked_for():
    batch = imports.usable([{"email": "", "phone_number": "+14165550100"}])
    assert batch.rows == [] and batch.skipped == [("", "no email (phone +14165550100)")]


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
    assert not [c for c in w.calls if c[0] == "subscribe"]


def test_suppress_submits_without_waiting(tmp_path):
    class NoWait(FakeWriter):
        def wait(self, jobs, *, timeout, progress):
            raise AssertionError("suppression jobs must not be waited on")
    w = NoWait()
    imp, log = importer(tmp_path, w, "suppress")
    imp.suppress([f"p{i}@example.com" for i in range(150)])
    assert log.counts["written"] == 150


@respx.mock
def test_suppress_batches_of_100_and_saves_emails():
    route = respx.post(f"{API}/profile-suppression-bulk-create-jobs/").mock(
        return_value=httpx.Response(202, json=job_body("S")))
    jobs = []
    rejected = writer().suppress([f"p{i}@example.com" for i in range(150)], on_job=jobs.append)
    assert [len(emails_in(c.request)) for c in route.calls] == [100, 50]
    assert rejected == [] and len(jobs[0].emails) == 100


@respx.mock
def test_wait_returns_unfinished_jobs_after_timeout():
    respx.get(f"{API}/profile-suppression-bulk-create-jobs/s1/").mock(
        return_value=httpx.Response(200, json={"data": {"attributes": {"status": "processing"}}}))
    job = Job("profile-suppression-bulk-create-jobs", "s1", 1)
    assert writer().wait([job], timeout=30) == [job]


@respx.mock
def test_existing_emails_uses_any_filter():
    route = respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json={
        "data": [{"attributes": {"email": "A@example.com"}}], "links": {"next": None}}))
    assert writer().existing_emails(["a@example.com", "b@example.com"]) == {"a@example.com"}
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


@respx.mock
def test_suppressions_check_reports_states(tmp_path, monkeypatch, klaviyo_account):
    klaviyo_account('T2aEdf')
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


@respx.mock
def test_already_subscribed_refusal_adds_to_list_without_error(tmp_path):
    def sub(request):
        emails = emails_in(request)
        if "old@example.com" in emails:
            i = emails.index("old@example.com")
            return httpx.Response(400, json={"errors": [{"status": 400, "title": "Invalid input.",
                "detail": "backdated consent date [2025-07-22 18:22:22+00:00] is after current subscription date [2020-02-21 05:29:23+00:00]",
                "source": {"pointer": f"/data/attributes/profiles/data/{i}/attributes/subscriptions/email/marketing/consented_at"}}]})
        return httpx.Response(202)
    respx.post(f"{API}/profile-subscription-bulk-create-jobs/").mock(side_effect=sub)
    imports_ = respx.post(f"{API}/profile-bulk-import-jobs/").mock(
        return_value=httpx.Response(202, json=job_body("L")))
    respx.get(f"{API}/profile-bulk-import-jobs/L/").mock(
        return_value=httpx.Response(200, json=job_body("L", "complete", completed_count=1, failed_count=0)))
    log = RunLog(new_run("klaviyo_sandbox", "t", base=tmp_path), echo=lambda _: None)
    imp = imports.Importer(writer(), log, echo=lambda _: None, save_job=lambda j: None, main_step="import")
    imp.subscribe([{"email": "new@example.com", "consent_timestamp": "2024-01-01T00:00:00Z"},
                   {"email": "old@example.com", "consent_timestamp": "2025-07-22T18:22:22Z"}], list_id="NEWS")
    assert log.counts["failed"] == 0
    assert imp.steps == {"subscribe": 1, "subscribe_list_only": 1}
    body = json.loads(imports_.calls.last.request.content)["data"]
    assert body["relationships"]["lists"]["data"] == [{"type": "list", "id": "NEWS"}]
    assert body["attributes"]["profiles"]["data"] == [{"type": "profile", "attributes": {"email": "old@example.com"}}]


@respx.mock
def test_other_subscribe_refusals_stay_errors(tmp_path):
    respx.post(f"{API}/profile-subscription-bulk-create-jobs/").mock(return_value=httpx.Response(400, json={"errors": [
        {"status": 400, "title": "Invalid input.", "detail": "backdated consent date [2020] is before current unsubscription date [2026]",
         "source": {"pointer": "/data/attributes/profiles/data/0/attributes/subscriptions/email/marketing/consented_at"}}]}))
    imports_ = respx.post(f"{API}/profile-bulk-import-jobs/")
    log = RunLog(new_run("klaviyo_sandbox", "t", base=tmp_path), echo=lambda _: None)
    imp = imports.Importer(writer(), log, echo=lambda _: None, save_job=lambda j: None, main_step="import")
    imp.subscribe([{"email": "u@example.com", "consent_timestamp": "2020-01-01T00:00:00Z"}], list_id="NEWS")
    assert log.counts["failed"] == 1 and imports_.call_count == 0


@pytest.mark.parametrize("text,message", [
    ("[{'reason': 'HARD_BOUNCE'}", "not valid JSON"),
    ('{"reason": "HARD_BOUNCE"}', "expected a list"),
    ('[{"timestamp": "2024-01-01"}]', "expected a list"),
    ('["HARD_BOUNCE"]', "expected a list"),
])
def test_malformed_suppressions_stop_the_row(tmp_path, text, message):
    row = {"email": "a@example.com", "consent": "SUBSCRIBED", "consent_timestamp": "2022-01-01T00:00:00Z",
           "suppression_reason": "", "suppressions": text}
    with pytest.raises(ValueError, match=message):
        imports.suppression_reasons(row)
    w = FakeWriter()
    imp, log = importer(tmp_path, w, "import")
    imports.profiles_import(imp, [row], list_id="L1", run_id="R")
    assert log.counts["failed"] == 1
    assert w.calls == []  # not imported, not subscribed


def test_empty_suppressions_list_means_no_suppression():
    assert imports.suppression_reasons({"suppressions": "[]", "suppression_reason": ""}) == []


def test_read_rows_trims_identifiers_but_keeps_text_verbatim(tmp_path):
    f = tmp_path / "in.csv"
    f.write_text('email,consent,properties.code,first_name,properties.blank\n'
                 '"  A@Example.com ", SUBSCRIBED ,"  ABC  ", Ann ,"   "\n')
    _, [row] = imports.read_rows(f)
    assert row["email"] == "A@Example.com" and row["consent"] == "SUBSCRIBED"
    assert row["properties.code"] == "  ABC  " and row["first_name"] == " Ann " and row["properties.blank"] == "   "
    attrs = profile_attributes(row, {})
    assert attrs["properties"]["code"] == "  ABC  "
