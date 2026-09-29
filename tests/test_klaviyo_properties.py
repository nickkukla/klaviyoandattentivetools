import csv
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import properties
from test_klaviyo_dedupe import API, mock_account_and_lists, write_csv


@pytest.mark.parametrize("text,kind,expected", [
    ("true", "bool", True), ("FALSE", "bool", False), ("3", "number", 3), ("2.5", "number", 2.5),
    ("true", "text", "true"),
])
def test_parse_value(text, kind, expected):
    value = properties.parse_value(text, kind)
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize("text,kind", [("yes", "bool"), ("nan", "number"), ("x", "json")])
def test_parse_value_refuses_bad_input(text, kind):
    with pytest.raises(ValueError):
        properties.parse_value(text, kind)


@pytest.mark.parametrize("key", ["", "  ", "$email"])
def test_check_key_refuses_empty_and_internal_names(key):
    with pytest.raises(ValueError):
        properties.check_key(key)


def test_plan_updates_existing_profiles_only_and_sends_just_the_property():
    rows = [{"email": "a@example.com", "first_name": "A"}, {"email": "new@example.com"}]
    keep, skipped = properties.plan(rows, lambda emails: {"a@example.com"})
    assert [r["email"] for r in keep] == ["a@example.com"]
    assert skipped == [("new@example.com", "no existing profile in the destination (update only)")]
    assert properties.payloads(keep, "catchup_hold", True) == [
        {"email": "a@example.com", "properties": {"catchup_hold": True}}]


def mock_import_job():
    imports_ = respx.post(f"{API}/profile-bulk-import-jobs/").mock(
        return_value=httpx.Response(202, json={"data": {"id": "J", "attributes": {"status": "queued"}}}))
    respx.get(f"{API}/profile-bulk-import-jobs/J/").mock(return_value=httpx.Response(200, json={
        "data": {"id": "J", "attributes": {"status": "complete", "completed_count": 1, "failed_count": 0}}}))
    return imports_


@respx.mock
def test_set_property_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    mock_account_and_lists(existing_emails=["a@example.com"])
    imports_ = mock_import_job()
    subs = respx.post(f"{API}/profile-subscription-bulk-create-jobs/").mock(return_value=httpx.Response(202))
    f = tmp_path / "hold.csv"
    write_csv(f, ["email"], [{"email": "A@example.com"}, {"email": "gone@example.com"}, {"email": ""},
                             {"email": "a@example.com"}])
    result = CliRunner().invoke(app, ["klaviyo", "profiles", "set-property", "--to", "klaviyo_sandbox",
                                      "--file", str(f), "--key", "catchup_hold", "--value", "true", "--yes"])
    assert result.exit_code == 0, result.output
    assert "catchup_hold = True (bool)" in result.output
    assert "1 existing profiles; 1 emails have no profile" in result.output
    assert "3 skipped" in result.output  # no profile, no email, duplicate
    body = json.loads(imports_.calls.last.request.content)["data"]
    assert "relationships" not in body or not body["relationships"].get("lists", {}).get("data")
    [profile] = body["attributes"]["profiles"]["data"]
    assert profile["attributes"] == {"email": "a@example.com", "properties": {"catchup_hold": True}}
    assert not subs.called
    [skipped] = (tmp_path / "exports/klaviyo_sandbox/set-property").glob("*.skipped.csv")
    assert "gone@example.com" in skipped.read_text()


@respx.mock
def test_set_property_refuses_a_bad_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "hold.csv"
    f.write_text("email\na@example.com\n")
    result = CliRunner().invoke(app, ["klaviyo", "profiles", "set-property", "--to", "klaviyo_sandbox",
                                      "--file", str(f), "--key", "catchup_hold", "--value", "yes", "--yes"])
    assert result.exit_code != 0 and "not true or false" in result.output


@respx.mock
def test_check_property_reports_wrong_and_missing_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    mock_account_and_lists()
    stored = {"ok@example.com": {"catchup_hold": True}, "text@example.com": {"catchup_hold": "true"},
              "unset@example.com": {}}

    def profiles_(request):
        flt = request.url.params.get("filter", "")
        data = [{"id": f"P{i}", "attributes": {"email": e, "properties": p}}
                for i, (e, p) in enumerate(stored.items()) if f'"{e}"' in flt]
        return httpx.Response(200, json={"data": data, "links": {"next": None}})
    respx.get(f"{API}/profiles/").mock(side_effect=profiles_)
    f = tmp_path / "hold.csv"
    write_csv(f, ["email"], [{"email": e} for e in [*stored, "gone@example.com"]])
    result = CliRunner().invoke(app, ["klaviyo", "profiles", "check-property", "--instance", "klaviyo_sandbox",
                                      "--file", str(f), "--key", "catchup_hold", "--value", "true"])
    assert result.exit_code == 1, result.output
    assert "checked 4: ok 1, no profile 1, mismatched 2" in result.output
    [mismatches] = (tmp_path / "exports/klaviyo_sandbox/check-property").glob("*.mismatches.csv")
    with open(mismatches, newline="") as fh:
        rows = {r["email"]: r["problem"] for r in csv.DictReader(fh)}
    assert rows == {"text@example.com": "catchup_hold is 'true', expected True",
                    "unset@example.com": "catchup_hold is None, expected True", "gone@example.com": "no profile"}
