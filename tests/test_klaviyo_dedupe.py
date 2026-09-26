import csv
import json
import re

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import dedupe

API = "https://a.klaviyo.com/api"
TYPES = {"Shopify Tags": "json", "Checked in": "bool", "Current Balance": "number", "coupon": "text"}


def plan(role, rows, existing=(), types=TYPES):
    found = set(existing)
    return dedupe.plan(dedupe.ROLES[role], rows, types, "RUN1",
                       lambda e, p: {x for x in e if x in found} | {x for x in p if x in found},
                       us_snapshot=found if role == "suppress" else None)


# --- Column translation --------------------------------------------------------

def test_payload_translates_the_ui_import_layout():
    row = {"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2022-01-01T00:00:00Z",
           "email": "a@example.com", "phone_number": "+14165550100", "first_name": "Ann", "last_name": "",
           "location_city": "Toronto", "location_latitude": "43.6", "market": "CA", "migration_source": "shopify_CA",
           "migration_hold": "TRUE", "ca_consent_method_detail": "Footer", "ca_consent": "SUBSCRIBED",
           "Shopify Tags": '["vip","swim"]', "Checked in": "false", "Current Balance": "12.5", "coupon": "123"}
    attrs = dedupe.payload(row, TYPES, {"migrated_from": "ca"}, only_hold=False)
    assert attrs["email"] == "a@example.com" and attrs["phone_number"] == "+14165550100"
    assert attrs["first_name"] == "Ann" and "last_name" not in attrs
    assert attrs["location"] == {"city": "Toronto", "latitude": 43.6}
    props = attrs["properties"]
    assert props["migration_hold"] is True and props["market"] == "CA"
    assert props["Shopify Tags"] == ["vip", "swim"] and props["Checked in"] is False
    assert props["Current Balance"] == 12.5 and props["coupon"] == "123"
    assert props["ca_consent_source"] == "Footer" and "ca_consent_method_detail" not in props
    assert "migration_source" not in props and props["migrated_from"] == "ca"
    assert props["ca_consent"] == "SUBSCRIBED"
    assert not {"Email Marketing Consent", "Email Marketing Consent Timestamp"} & set(props)


def test_hold_payload_sends_only_migration_hold():
    row = {"email": "a@example.com", "migration_hold": "false", "first_name": "X", "market": "CA"}
    assert dedupe.payload(row, {}, {}, only_hold=True) == {"email": "a@example.com",
                                                          "properties": {"migration_hold": False}}


@pytest.mark.parametrize("text,expected", [("Subscribe", "SUBSCRIBED"), ("subscribed", "SUBSCRIBED"),
                                           ("Unsubscribed", "UNSUBSCRIBED"), ("", None)])
def test_consent_instruction(text, expected):
    assert dedupe.consent_instruction({"Email Marketing Consent": text}) == expected


def test_ca_consent_is_never_an_instruction():
    # 03d carries ca_consent=SUBSCRIBED on suppressed rows; it must not subscribe them.
    p = plan("new", [{"email": "s@example.com", "ca_consent": "SUBSCRIBED", "migration_hold": "true"}])
    assert p.instructions == [None]


def test_consent_timestamp_falls_back_to_ca_consent_timestamp():
    assert dedupe.consent_timestamp({"ca_consent_timestamp": "2024-08-09T12:22:29Z"}) == "2024-08-09T12:22:29Z"


# --- Planning ------------------------------------------------------------------

def test_plan_rules_by_role():
    rows = [{"email": "old@example.com", "migration_hold": "true"}, {"email": "new@example.com", "migration_hold": "true"},
            {"email": "", "phone_number": "+14165550100", "migration_hold": "true"}]
    hold = plan("hold", [dict(r) for r in rows], existing={"old@example.com", "+14165550100"})
    assert [dedupe.identity(r) for r in hold.rows] == ["old@example.com", "+14165550100"]
    assert hold.skipped == [("new@example.com", "no existing profile in the destination (update only)")]
    assert all("migrated_from" not in a["properties"] for a in hold.payloads)
    held_new = plan("hold-new", [dict(r) for r in rows])
    assert len(held_new.rows) == 3 and all(a["properties"]["migration_run_id"] == "RUN1" for a in held_new.payloads)


def test_suppress_tags_only_profiles_it_creates():
    rows = [{"email": "us@example.com", "ca_suppression_reason": "HARD_BOUNCE"},
            {"email": "new@example.com", "ca_suppression_reason": "SPAM_COMPLAINT"}]
    p = plan("suppress", rows, existing={"us@example.com"})
    by = {a["email"]: a["properties"] for a in p.payloads}
    assert "migrated_from" not in by["us@example.com"] and by["new@example.com"]["migrated_from"] == "ca"
    assert (p.updates, p.creates) == (1, 1)


def test_plan_skips_and_flags_bad_rows():
    rows = [{"email": "A@Example.com", "Email Marketing Consent": "Subscribe"},
            {"email": "a@example.com"},
            {"email": "", "phone_number": ""},
            {"email": "", "phone_number": "14165550100"},
            {"email": "", "phone_number": "+14165550101", "Email Marketing Consent": "Subscribe"},
            {"email": "b@example.com", "Email Marketing Consent": "Maybe"},
            {"email": "c@example.com", "Checked in": "sometimes"}]
    p = plan("new", rows)
    assert [dedupe.identity(r) for r in p.rows] == ["a@example.com"]
    assert [reason for _, reason in p.skipped] == [
        "duplicate in file", "no email or phone number", "phone number isn't in +international format"]
    reasons = dict(p.unreadable)
    assert "needs an email" in reasons["+14165550101"]
    assert "not Subscribe or Unsubscribed" in reasons["b@example.com"]
    assert "Checked in" in reasons["c@example.com"]


# --- Carrying out a plan ---------------------------------------------------------

class FakeImporter:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def import_profiles(self, payloads, *, list_id, stage):
        self.calls.append(("import", [dedupe.identity(a) for a in payloads], list_id, stage))
        return {dedupe.identity(a) for a in payloads} - self.fail

    def skip_unconfirmed(self, rows, ok, step):
        return [r for r in rows if dedupe.identity(r) in ok]

    def subscribe(self, rows, *, list_id):
        self.calls.append(("subscribe", [(r["email"], r["consent_timestamp"]) for r in rows], list_id))

    def unsubscribe(self, emails):
        self.calls.append(("unsubscribe", list(emails)))

    def suppress(self, emails):
        self.calls.append(("suppress", list(emails)))


def test_run_new_joins_list_then_subscribes_confirmed_rows_to_newsletter():
    rows = [{"email": "s@example.com", "Email Marketing Consent": "Subscribe",
             "Email Marketing Consent Timestamp": "2023-01-01T00:00:00Z"},
            {"email": "f@example.com", "Email Marketing Consent": "Subscribe", "ca_consent_timestamp": "2023-02-02T00:00:00Z"},
            {"email": "u@example.com", "Email Marketing Consent": "Unsubscribed"},
            {"email": "", "phone_number": "+14165550100"}]
    imp = FakeImporter(fail={"f@example.com"})
    dedupe.run(imp, plan("new", rows), join_list="T7TTAp", subscribe_list="XrGL9u")
    assert imp.calls == [
        ("import", ["s@example.com", "f@example.com", "u@example.com", "+14165550100"], "T7TTAp", "import"),
        ("subscribe", [("s@example.com", "2023-01-01T00:00:00Z")], "XrGL9u"),
        ("unsubscribe", ["u@example.com"]),
    ]


def test_run_hold_joins_no_list_and_changes_no_consent():
    imp = FakeImporter()
    dedupe.run(imp, plan("hold", [{"email": "a@example.com", "migration_hold": "true"}], existing={"a@example.com"}),
               join_list=None, subscribe_list=None)
    assert imp.calls == [("import", ["a@example.com"], None, "update")]


def test_run_suppress_updates_existing_into_list_creates_new_then_suppresses_all():
    rows = [{"email": "us@example.com", "ca_suppression_reason": "HARD_BOUNCE"},
            {"email": "new@example.com", "ca_suppression_reason": "SPAM_COMPLAINT"}]
    imp = FakeImporter()
    dedupe.run(imp, plan("suppress", rows, existing={"us@example.com"}), join_list="Sc9zHg", subscribe_list=None)
    assert imp.calls == [("import", ["us@example.com"], "Sc9zHg", "update"),
                         ("import", ["new@example.com"], None, "create"),
                         ("suppress", ["us@example.com", "new@example.com"])]


# --- The command end to end, against mocked Klaviyo -------------------------------------

def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, columns)
        w.writeheader()
        w.writerows(rows)


def mock_account_and_lists(existing_emails=()):
    respx.get(f"{API}/accounts/").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "T2aEdf", "attributes": {"contact_information": {"organization_name": "Dev"}}}]}))
    respx.get(url__regex=rf"{API}/lists/\w+/").mock(
        return_value=httpx.Response(200, json={"data": {"attributes": {"name": "A list"}}}))

    def profiles(request):
        flt = request.url.params.get("filter", "")
        data = [{"attributes": {"email": e, "phone_number": None}} for e in existing_emails if f'"{e}"' in flt]
        return httpx.Response(200, json={"data": data, "links": {"next": None}})
    respx.get(f"{API}/profiles/").mock(side_effect=profiles)


@respx.mock
def test_kept_command_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    mock_account_and_lists(existing_emails=["k@example.com"])
    imports_ = respx.post(f"{API}/profile-bulk-import-jobs/").mock(
        return_value=httpx.Response(202, json={"data": {"id": "J", "attributes": {"status": "queued"}}}))
    respx.get(f"{API}/profile-bulk-import-jobs/J/").mock(return_value=httpx.Response(200, json={
        "data": {"id": "J", "attributes": {"status": "complete", "completed_count": 1, "failed_count": 0}}}))
    subs = respx.post(f"{API}/profile-subscription-bulk-create-jobs/").mock(return_value=httpx.Response(202))
    f = tmp_path / "04a.csv"
    write_csv(f, ["Email Marketing Consent", "email", "market", "migration_source", "migration_hold", "ca_consent_timestamp"],
              [{"Email Marketing Consent": "Subscribe", "email": "k@example.com", "market": "CA",
                "migration_source": "shopify_CA", "migration_hold": "true", "ca_consent_timestamp": "2024-08-09T12:22:29Z"},
               {"Email Marketing Consent": "Subscribe", "email": "gone@example.com", "market": "CA",
                "migration_source": "shopify_CA", "migration_hold": "true", "ca_consent_timestamp": "2024-08-09T12:22:29Z"}])
    result = CliRunner().invoke(app, ["klaviyo", "dedupe", "import", "--to", "klaviyo_sandbox", "--file", str(f),
                                      "--role", "kept", "--join-list", "Sc9zHg", "--subscribe-list", "XrGL9u", "--yes"])
    assert result.exit_code == 0, result.output
    assert "1 existing, 0 new" in result.output and "1 skipped" in result.output
    body = json.loads(imports_.calls.last.request.content)["data"]
    assert body["relationships"]["lists"]["data"] == [{"type": "list", "id": "Sc9zHg"}]
    [profile] = body["attributes"]["profiles"]["data"]
    assert profile["attributes"]["properties"]["migrated_from"] == "ca"
    assert profile["attributes"]["properties"]["migration_hold"] is True
    sub = json.loads(subs.calls.last.request.content)["data"]
    assert sub["relationships"]["list"]["data"]["id"] == "XrGL9u" and sub["attributes"]["historical_import"] is True
    assert sub["attributes"]["profiles"]["data"][0]["attributes"]["subscriptions"]["email"]["marketing"][
        "consented_at"] == "2024-08-09T12:22:29Z"
    [skipped] = (tmp_path / "exports/klaviyo_sandbox/dedupe-kept").glob("*.skipped.csv")
    assert "gone@example.com" in skipped.read_text()


@pytest.mark.parametrize("args,message", [
    (["--role", "hold", "--join-list", "X"], "doesn't join a list"),
    (["--role", "new"], "needs --join-list"),
    (["--role", "new", "--join-list", "X"], "needs --types-from"),
    (["--role", "suppress", "--join-list", "X"], "needs --us-snapshot"),
    (["--role", "hold-new", "--subscribe-list", "X"], "doesn't subscribe"),
    (["--role", "nope"], "isn't one of"),
])
def test_role_option_checks(tmp_path, monkeypatch, args, message):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "f.csv"
    f.write_text("email,migration_hold\na@example.com,true\n")
    result = CliRunner().invoke(app, ["klaviyo", "dedupe", "import", "--to", "klaviyo_sandbox", "--file", str(f), *args])
    plain = " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", result.output)).split())
    assert result.exit_code != 0 and message in plain


def test_type_map_reads_export_header(tmp_path):
    f = tmp_path / "export.csv"
    f.write_text("id,email,properties.Shopify Tags#json,properties.coupon,properties.Checked in#bool\n")
    assert dedupe.type_map(f) == {"Shopify Tags": "json", "coupon": "text", "Checked in": "bool"}


def test_suppress_split_comes_from_the_us_snapshot_not_live_state(tmp_path):
    # created@ exists live (an earlier run, or the suppression call itself,
    # created it, possibly untagged) but isn't in the pre-migration US export,
    # so it stays CA-only: tagged, and never put on the Updated US list.
    snap = tmp_path / "us.csv"
    snap.write_text("id,email,phone_number\nU1,US@example.com,\nU2,,+14165550100\n")
    snapshot = dedupe.load_snapshot(snap)
    assert snapshot == {"us@example.com", "+14165550100"}
    rows = [{"email": "us@example.com", "ca_suppression_reason": "HARD_BOUNCE"},
            {"email": "created@example.com", "ca_suppression_reason": "SPAM_COMPLAINT"}]
    p = dedupe.plan(dedupe.ROLES["suppress"], rows, {}, "RUN2",
                    lambda e, ph: {"us@example.com", "created@example.com"}, us_snapshot=snapshot)
    assert p.existing == {"us@example.com"} and (p.updates, p.creates) == (1, 1)
    by = {a["email"]: a["properties"] for a in p.payloads}
    assert "migrated_from" not in by["us@example.com"] and by["created@example.com"]["migrated_from"] == "ca"
    imp = FakeImporter()
    dedupe.run(imp, p, join_list="Sc9zHg", subscribe_list=None)
    assert imp.calls[:2] == [("import", ["us@example.com"], "Sc9zHg", "update"),
                             ("import", ["created@example.com"], None, "create")]


def test_suppress_without_snapshot_is_refused():
    with pytest.raises(ValueError, match="snapshot"):
        dedupe.plan(dedupe.ROLES["suppress"], [{"email": "a@example.com"}], {}, "R", lambda e, p: set())



# --- Checking an import ------------------------------------------------------------

def stored(consent="NEVER_SUBSCRIBED", suppression=(), ts=None, pid="P1", props=None, **fields):
    """Profile attributes as fetch_profiles returns them."""
    return {"_id": pid, **fields,
            "subscriptions": {"email": {"marketing": {"consent": consent, "consent_timestamp": ts,
                                                      "suppression": [{"reason": r} for r in suppression]}}},
            "properties": props or {}}


def problems(role, row, attrs, *, join=None, subscribe=None, us_profile=False, types=TYPES):
    return dedupe.problems(dedupe.ROLES[role], row, attrs, types=types, us_profile=us_profile,
                           join=join, subscribe=subscribe)


def test_hold_checks_the_hold_value_only():
    assert problems("hold", {"email": "a@x.com", "migration_hold": "true"}, stored(props={"migration_hold": True})) == []
    assert problems("hold", {"email": "a@x.com", "migration_hold": "false"}, stored(props={"migration_hold": True})) == [
        "property migration_hold is True, expected False"]
    assert problems("hold", {"email": "a@x.com", "migration_hold": "true"}, None) == ["no profile"]


def test_new_subscribed_row_that_landed():
    row = {"email": "a@x.com", "Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2022-05-05T05:00:00Z",
           "first_name": "Ann", "market": "CA", "Shopify Tags": '["vip"]', "migration_hold": "true"}
    attrs = stored("SUBSCRIBED", ts="2022-05-05T05:00:00+00:00", first_name="Ann",
                   props={"market": "CA", "Shopify Tags": ["vip"], "migration_hold": True, "migrated_from": "ca"})
    assert problems("new", row, attrs, join={"P1"}, subscribe={"P1"}) == []


def test_new_subscribed_row_that_did_not_land():
    row = {"email": "a@x.com", "Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2022-05-05T05:00:00Z",
           "market": "CA"}
    attrs = stored("NEVER_SUBSCRIBED", props={"market": "US"})
    assert problems("new", row, attrs, join=set(), subscribe=set()) == [
        "property market is 'US', expected 'CA'", "missing migrated_from=ca tag", "not on the join list",
        "consent is NEVER_SUBSCRIBED, expected SUBSCRIBED", "not on the subscribe list",
        "consent_timestamp is None, expected 2022-05-05T05:00:00Z"]


def test_subscribed_but_still_suppressed_is_flagged():
    row = {"email": "a@x.com", "Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2022-05-05T05:00:00Z"}
    attrs = stored("SUBSCRIBED", suppression=["USER_SUPPRESSED"], ts="2022-05-05T05:00:00Z", props={"migrated_from": "ca"})
    assert problems("new", row, attrs, join={"P1"}, subscribe={"P1"}) == [
        "subscribed but still suppressed (USER_SUPPRESSED): won't receive email"]


def test_timezone_is_only_checked_when_klaviyo_does_not_recalculate_it():
    # With coordinates, Klaviyo sets the timezone from them (None when they
    # contradict the country), so only the coordinates are compared.
    row = {"email": "a@x.com", "location_city": "Ottawa", "location_latitude": "45.3", "location_timezone": "America/Toronto"}
    attrs = stored(location={"city": "Ottawa", "latitude": 45.3, "timezone": "America/Montreal"}, props={"migrated_from": "ca"})
    assert problems("new", row, attrs, join={"P1"}) == []
    row = {"email": "a@x.com", "location_city": "Ottawa", "location_timezone": "America/Toronto"}
    attrs = stored(location={"city": "Ottawa", "timezone": None}, props={"migrated_from": "ca"})
    assert problems("new", row, attrs, join={"P1"}) == ["location.timezone is None, expected 'America/Toronto'"]


def test_subscribe_date_rules():
    new_row = {"email": "a@x.com", "Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2022-05-05T05:00:00Z"}
    ok = stored("SUBSCRIBED", ts="2022-05-05T05:00:00+00:00", props={"migrated_from": "ca"})
    off = stored("SUBSCRIBED", ts="2026-01-01T00:00:00+00:00", props={"migrated_from": "ca"})
    base = dict(join={"P1"}, subscribe={"P1"})
    assert problems("new", new_row, ok, **base) == []
    assert problems("new", new_row, off, **base) == [
        "consent_timestamp is 2026-01-01T00:00:00+00:00, expected 2022-05-05T05:00:00Z"]
    # kept: an existing subscriber keeps its own date, earlier or later.
    kept_row = {"email": "a@x.com", "Email Marketing Consent": "Subscribe", "ca_consent_timestamp": "2024-08-09T12:22:29Z"}
    props = {"migrated_from": "ca", "ca_consent_timestamp": "2024-08-09T12:22:29Z"}
    for ts in ("2020-02-21T05:29:23Z", "2025-01-01T00:00:00Z"):
        assert problems("kept", kept_row, stored("SUBSCRIBED", ts=ts, props=props), **base) == []


def test_03d_row_must_be_suppressed():
    row = {"email": "a@x.com", "ca_consent": "SUBSCRIBED", "ca_suppression_reason": "SPAM_COMPLAINT", "migration_hold": "true"}
    props = {"ca_consent": "SUBSCRIBED", "ca_suppression_reason": "SPAM_COMPLAINT", "migration_hold": True, "migrated_from": "ca"}
    assert problems("new", row, stored(props=props), join={"P1"}) == ["not suppressed (expected SPAM_COMPLAINT)"]
    assert problems("new", row, stored(suppression=["USER_SUPPRESSED"], props=props), join={"P1"}) == []
    # An unsubscribe alone doesn't count as the expected suppression.
    assert problems("new", row, stored(suppression=["UNSUBSCRIBE"], props=props), join={"P1"}) == [
        "not suppressed (expected SPAM_COMPLAINT)"]


def test_kept_row_missing_market_and_audit_values_is_flagged():
    row = {"email": "a@x.com", "Email Marketing Consent": "Unsubscribed", "market": "CA", "migration_hold": "true",
           "ca_consent": "UNSUBSCRIBED", "ca_consent_method_detail": "Footer"}
    attrs = stored("UNSUBSCRIBED", props={"migration_hold": True, "migrated_from": "ca"})
    assert problems("kept", row, attrs, join={"P1"}) == [
        "property market is None, expected 'CA'", "property ca_consent is None, expected 'UNSUBSCRIBED'",
        "property ca_consent_source is None, expected 'Footer'"]


def test_suppress_rules_depend_on_the_us_snapshot():
    row = {"email": "a@x.com", "ca_suppression_reason": "HARD_BOUNCE"}
    us = stored(suppression=["HARD_BOUNCE"], props={"ca_suppression_reason": "HARD_BOUNCE"})
    assert problems("suppress", row, us, join={"P1"}, us_profile=True) == []
    assert problems("suppress", row, us, join=set(), us_profile=True) == ["not on the join list"]
    ca_only = stored(suppression=["HARD_BOUNCE"], props={"ca_suppression_reason": "HARD_BOUNCE"})
    assert problems("suppress", row, ca_only, join=set(), us_profile=False) == ["missing migrated_from=ca tag"]


def test_number_types_compare_by_value():
    assert dedupe._same(3, 3.0) and not dedupe._same(True, 1) and dedupe._same(["a"], ["a"])


def check_cli(tmp_path, args, klaviyo_account, profiles_data, list_ids=()):
    klaviyo_account("T2aEdf")

    def profiles(request):
        flt = request.url.params.get("filter", "")
        data = [p for p in profiles_data if any(f'"{v}"' in flt for v in (p["attributes"].get("email"), p["attributes"].get("phone_number")) if v)]
        return httpx.Response(200, json={"data": data, "links": {"next": None}})
    respx.get(f"{API}/profiles/").mock(side_effect=profiles)
    respx.get(url__regex=rf"{API}/lists/\w+/profiles/").mock(return_value=httpx.Response(200, json={
        "data": [{"id": i, "attributes": {"email": None}} for i in list_ids], "links": {"next": None}}))
    return CliRunner().invoke(app, ["klaviyo", "dedupe", "check", "--instance", "klaviyo_sandbox", *args])


@respx.mock
def test_check_finds_phone_only_rows_whose_profile_also_has_an_email(tmp_path, monkeypatch, klaviyo_account):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    f = tmp_path / "01.csv"
    f.write_text("email,phone_number,migration_hold\n,+14165550100,true\n")
    data = [{"id": "P9", "attributes": {"email": "has-email@example.com", "phone_number": "+14165550100",
                                         **{k: v for k, v in stored(props={"migration_hold": True}).items() if k != "_id"}}}]
    result = check_cli(tmp_path, ["--role", "hold", "--file", str(f)], klaviyo_account, data)
    assert result.exit_code == 0, result.output
    assert "ok 1, mismatched 0" in result.output


@respx.mock
def test_check_command_writes_mismatches_and_fails(tmp_path, monkeypatch, klaviyo_account):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    f = tmp_path / "05.csv"
    f.write_text("email,phone_number,migration_hold\nok@example.com,,false\nmissing@example.com,,false\n")
    data = [{"id": "P1", "attributes": {"email": "ok@example.com", "phone_number": None,
                                         **{k: v for k, v in stored(props={"migration_hold": False}).items() if k != "_id"}}}]
    result = check_cli(tmp_path, ["--role", "hold", "--file", str(f)], klaviyo_account, data)
    assert result.exit_code == 1
    assert "ok 1, mismatched 1" in result.output
    [m] = (tmp_path / "exports/klaviyo_sandbox/dedupe-check-hold").glob("*.mismatches.csv")
    assert "missing@example.com,no profile" in m.read_text()


@pytest.mark.parametrize("args,message", [
    (["--role", "new"], "needs --join-list"),
    (["--role", "kept"], "needs --join-list"),
    (["--role", "kept", "--join-list", "X"], "give --subscribe-list"),
    (["--role", "suppress", "--join-list", "X"], "needs --us-snapshot"),
])
def test_check_needs_the_same_options_as_the_import(tmp_path, monkeypatch, args, message):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "f.csv"
    f.write_text("Email Marketing Consent,email,migration_hold\nSubscribe,a@example.com,true\n")
    result = CliRunner().invoke(app, ["klaviyo", "dedupe", "check", "--instance", "klaviyo_sandbox", "--file", str(f), *args])
    plain = " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", result.output)).split())
    assert result.exit_code != 0 and message in plain


@respx.mock
def test_ambiguous_retry_is_recorded_and_gates_the_next_import(tmp_path, monkeypatch, klaviyo_account):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    monkeypatch.setattr("migtool.http.time.sleep", lambda s: None)
    klaviyo_account("T2aEdf")
    respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json={
        "data": [{"attributes": {"email": "a@example.com", "phone_number": None}}], "links": {"next": None}}))
    jobs = respx.post(f"{API}/profile-bulk-import-jobs/").mock(side_effect=[
        httpx.ReadTimeout("response lost"), httpx.Response(202, json={"data": {"id": "J", "attributes": {"status": "queued"}}}),
        httpx.Response(202, json={"data": {"id": "J2", "attributes": {"status": "queued"}}})])
    respx.get(url__regex=rf"{API}/profile-bulk-import-jobs/J2?/").mock(return_value=httpx.Response(200, json={
        "data": {"attributes": {"status": "complete", "completed_count": 1, "failed_count": 0}}}))
    f = tmp_path / "01.csv"
    f.write_text("email,phone_number,migration_hold\na@example.com,,true\n")
    cmd = ["klaviyo", "dedupe", "import", "--to", "klaviyo_sandbox", "--role", "hold", "--file", str(f), "--yes"]
    first = CliRunner().invoke(app, cmd)
    assert first.exit_code == 0, first.output
    saved = json.loads((tmp_path / "state/klaviyo_sandbox/ambiguous_writes.json").read_text())
    assert len(saved) == 1 and "ReadTimeout" in saved[0]["message"]
    second = CliRunner().invoke(app, cmd)
    assert second.exit_code != 0 and "retried after a lost response" in str(second.exception)
    assert jobs.call_count == 2  # the gated run sent nothing
    third = CliRunner().invoke(app, [*cmd, "--retries-settled"])
    assert third.exit_code == 0, third.output
    assert "Settled:" in third.output and not (tmp_path / "state/klaviyo_sandbox/ambiguous_writes.json").exists()
