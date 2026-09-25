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
                       lambda e, p: {x for x in e if x in found} | {x for x in p if x in found})


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
