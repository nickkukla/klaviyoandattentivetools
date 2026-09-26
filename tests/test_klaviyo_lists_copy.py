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
    assert parse_qs(urlparse(str(named.calls.last.request.url)).query)["filter"] == ['equals(name,"VIP (CA)")']
    assert json.loads(create.calls.last.request.content)["data"]["attributes"] == {"name": "VIP (CA)"}
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
        {"id": "X1", "attributes": {"name": "VIP (CA)"}}])))
    create = respx.post(f"{API}/lists/")
    result = run("--create")
    assert result.exit_code != 0
    assert "already has a list named 'VIP (CA)'" in str(result.exception)
    assert not create.called and not calls


@pytest.mark.parametrize("args", [[], ["--create", "--to-list", "T9"], ["--to-list", "T9", "--name", "X"]])
def test_copy_needs_exactly_one_destination(args, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = run(*args)
    assert result.exit_code != 0


@respx.mock
def test_copy_end_to_end_recovers_a_lost_create_response(tmp_path, monkeypatch, klaviyo_account):
    """The real writer against mocked HTTP: the create's response is lost, so
    it isn't retried; the list is found by name and the members go there."""
    klaviyo_account("T2aEdf")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    respx.get(f"{API}/lists/L1/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "L1", "type": "list", "attributes": {"name": 'VIP "Canada"'}}}))
    respx.get(f"{API}/lists/L1/profiles/").mock(return_value=httpx.Response(200, json=page([
        {"id": "p1", "attributes": {"email": "a@example.com", "joined_group_at": None}}])))
    named = respx.get(f"{API}/lists/").mock(side_effect=[
        httpx.Response(200, json=page([])),
        httpx.Response(200, json=page([{"id": "NEW", "attributes": {"name": 'VIP "Canada" (CA)'}}])),
    ])
    create = respx.post(f"{API}/lists/").mock(return_value=httpx.Response(504))
    respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json=page([
        {"id": "u1", "attributes": {"email": "a@example.com", "phone_number": None}}])))
    job = respx.post(f"{API}/profile-bulk-import-jobs/").mock(return_value=httpx.Response(202, json={"data": {
        "id": "J1", "attributes": {"status": "queued"}}}))
    respx.get(f"{API}/profile-bulk-import-jobs/J1/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "J1", "attributes": {"status": "complete", "completed_count": 1, "failed_count": 0}}}))
    result = run("--create")
    assert result.exit_code == 0, result.output
    assert create.call_count == 1  # not retried
    assert parse_qs(urlparse(str(named.calls[0].request.url)).query)["filter"] == ['equals(name,"VIP \\"Canada\\" (CA)")']
    body = json.loads(job.calls.last.request.content)["data"]
    assert body["relationships"]["lists"]["data"] == [{"type": "list", "id": "NEW"}]
    assert [p["attributes"] for p in body["attributes"]["profiles"]["data"]] == [{"email": "a@example.com"}]
    # The uncertain create is recorded, so the next write waits for confirmation.
    assert len(json.loads((tmp_path / "state/klaviyo_sandbox/ambiguous_writes.json").read_text())) == 1


@respx.mock
def test_copy_name_and_suffix_options(copy_env):
    respx.get(f"{API}/lists/").mock(return_value=httpx.Response(200, json=page([])))
    create = respx.post(f"{API}/lists/").mock(return_value=httpx.Response(201, json={"data": {"id": "NEW"}}))
    assert run("--create", "--suffix", " - from CA").exit_code == 0
    assert json.loads(create.calls.last.request.content)["data"]["attributes"]["name"] == "VIP - from CA"
    assert run("--create", "--name", "Exactly this").exit_code == 0
    assert json.loads(create.calls.last.request.content)["data"]["attributes"]["name"] == "Exactly this"
