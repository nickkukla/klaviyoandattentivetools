import csv
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import imports

API = "https://a.klaviyo.com/api"


def page(data):
    return {"data": data, "links": {"next": None}}


@pytest.fixture
def copy_env(tmp_path, monkeypatch, klaviyo_account):
    """Sandbox to sandbox, with lists_add stubbed to record what it was given."""
    klaviyo_account("T2aEdf")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    respx.get(f"{API}/lists/L1/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "L1", "type": "list", "attributes": {"name": "VIP"}}}))
    respx.get(f"{API}/lists/L1/profiles/").mock(return_value=httpx.Response(200, json=page([
        {"id": "p1", "attributes": {"email": "a@example.com", "joined_group_at": "2026-09-02T00:00:00+00:00"}},
        {"id": "p2", "attributes": {"email": None, "joined_group_at": "2026-09-02T00:00:00+00:00"}},
    ])))
    calls = []

    def fake_lists_add(imp, rows, columns, *, list_id, run_id):
        calls.append({"rows": rows, "columns": columns, "list_id": list_id})
        return 0

    monkeypatch.setattr(imports, "lists_add", fake_lists_add)
    return tmp_path, calls


def run(*args):
    return CliRunner().invoke(app, ["klaviyo", "lists", "copy", "--from", "klaviyo_sandbox", "--list", "L1",
                                    "--to", "klaviyo_sandbox", "--yes", *args])


@respx.mock
def test_copy_creates_the_list_and_adds_members_by_email_only(copy_env):
    tmp_path, calls = copy_env
    named = respx.get(f"{API}/lists/").mock(return_value=httpx.Response(200, json=page([])))
    create = respx.post(f"{API}/lists/").mock(return_value=httpx.Response(201, json={"data": {"id": "NEW"}}))
    result = run("--create")
    assert result.exit_code == 0, result.output
    assert "2 members, 1 without an email (skipped)" in result.output
    assert parse_qs(urlparse(str(named.calls.last.request.url)).query)["filter"] == ['equals(name,"VIP")']
    assert json.loads(create.calls.last.request.content)["data"]["attributes"] == {"name": "VIP"}
    # Only the email goes to lists_add, so nothing else is written to the profile.
    assert calls == [{"rows": [{"email": "a@example.com"}], "columns": ["email"], "list_id": "NEW"}]
    [manifest] = (tmp_path / "exports/klaviyo_sandbox/lists-copy").glob("manifest.json")
    last = json.loads(manifest.read_text())["runs"][-1]
    assert last["list_id"] == "NEW" and last["source_list"] == "L1"
    [members] = (tmp_path / "exports/klaviyo_sandbox/lists-copy").glob("*.members.csv")
    assert list(csv.DictReader(members.open())) == [{"email": "a@example.com"}]


@respx.mock
def test_copy_into_an_existing_list_creates_nothing(copy_env):
    _, calls = copy_env
    respx.get(f"{API}/lists/T9/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "T9", "type": "list", "attributes": {"name": "VIP US"}}}))
    create = respx.post(f"{API}/lists/")
    result = run("--to-list", "T9")
    assert result.exit_code == 0, result.output
    assert "Add to:          VIP US (T9)" in result.output
    assert not create.called
    assert calls[0]["list_id"] == "T9"


@respx.mock
def test_copy_refuses_to_create_a_list_whose_name_is_taken(copy_env):
    _, calls = copy_env
    respx.get(f"{API}/lists/").mock(return_value=httpx.Response(200, json=page([
        {"id": "X1", "attributes": {"name": "VIP"}}])))
    create = respx.post(f"{API}/lists/")
    result = run("--create")
    assert result.exit_code != 0
    assert "already has a list named 'VIP'" in str(result.exception)
    assert not create.called and not calls


@pytest.mark.parametrize("args", [[], ["--create", "--to-list", "T9"], ["--to-list", "T9", "--name", "X"]])
def test_copy_needs_exactly_one_destination(args, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = run(*args)
    assert result.exit_code != 0
