import json

import httpx
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import imports

API = "https://a.klaviyo.com/api"


def setup(tmp_path, monkeypatch, klaviyo_account, jobs):
    klaviyo_account("T2aEdf")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    state = tmp_path / "state/klaviyo_sandbox"
    state.mkdir(parents=True)
    (state / "jobs.json").write_text(json.dumps(jobs))
    (tmp_path / "members.csv").write_text("email\na@example.com\n")
    respx.get(f"{API}/lists/T9/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "T9", "attributes": {"name": "VIP"}}}))
    added = []
    monkeypatch.setattr(imports, "lists_add", lambda imp, rows, columns, **kw: added.append(rows) or 0)
    return state, added


def lists_add():
    return CliRunner().invoke(app, ["klaviyo", "lists", "add", "--to", "klaviyo_sandbox", "--list", "T9",
                                    "--file", "members.csv", "--yes"])


@respx.mock
def test_a_job_still_processing_stops_the_next_write(tmp_path, monkeypatch, klaviyo_account):
    _, added = setup(tmp_path, monkeypatch, klaviyo_account, [
        {"id": "J1", "kind": "profile-bulk-import-jobs", "run_id": "R1", "size": 5}])
    respx.get(f"{API}/profile-bulk-import-jobs/J1/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "J1", "attributes": {"status": "processing"}}}))
    result = lists_add()
    assert result.exit_code != 0
    assert "still processing" in str(result.exception) and "R1" in str(result.exception)
    assert added == []


@respx.mock
def test_finished_jobs_are_recorded_and_looked_up_once(tmp_path, monkeypatch, klaviyo_account):
    state, added = setup(tmp_path, monkeypatch, klaviyo_account, [
        {"id": "J1", "kind": "profile-bulk-import-jobs", "run_id": "R1", "size": 5},
        {"id": "J2", "kind": "profile-bulk-import-jobs", "run_id": "R1", "size": 5},
        {"id": "S1", "kind": "profile-suppression-bulk-create-jobs", "run_id": "R2", "size": 5}])
    j1 = respx.get(f"{API}/profile-bulk-import-jobs/J1/").mock(return_value=httpx.Response(200, json={"data": {
        "id": "J1", "attributes": {"status": "complete"}}}))
    respx.get(f"{API}/profile-bulk-import-jobs/J2/").mock(return_value=httpx.Response(404))
    assert lists_add().exit_code == 0
    assert added == [[{"email": "a@example.com"}]]
    saved = {j["id"]: j.get("status") for j in json.loads((state / "jobs.json").read_text())}
    # Suppression jobs aren't gated: their status isn't reliable.
    assert saved == {"J1": "complete", "J2": "not found", "S1": None}
    assert lists_add().exit_code == 0
    assert j1.call_count == 1
