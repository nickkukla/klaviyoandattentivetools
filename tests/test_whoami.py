import json
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from migtool.cli import app

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@respx.mock
def test_klaviyo_whoami(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_topsecret")
    route = respx.get("https://a.klaviyo.com/api/accounts/").mock(
        return_value=httpx.Response(200, json=load("klaviyo/accounts.json"))
    )
    result = CliRunner().invoke(app, ["klaviyo", "whoami", "--instance", "klaviyo_sandbox"])
    assert result.exit_code == 0, result.output
    assert "account T2aEdf (Left In Friday Dev)" in result.output
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Klaviyo-API-Key pk_topsecret"
    assert request.headers["revision"]


def test_whoami_rejects_unknown_instance(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["klaviyo", "whoami", "--instance", "klaviyo_uk"])
    assert result.exit_code != 0


@respx.mock
def test_key_for_the_wrong_account_is_refused(monkeypatch, tmp_path, klaviyo_account):
    """A CA key stored under the US variable must be caught before anything runs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_US_API_KEY", "pk_actually_the_ca_key")
    klaviyo_account("Ka6Lvr", "Left On Friday Canada")
    writes = respx.post(url__startswith="https://a.klaviyo.com/api/").mock(return_value=httpx.Response(202))
    f = tmp_path / "s.csv"
    f.write_text("email\na@example.com\n")
    for args in (["klaviyo", "whoami", "--instance", "klaviyo_us"],
                 ["klaviyo", "suppressions", "import", "--to", "klaviyo_us", "--file", str(f), "--yes"],
                 ["klaviyo", "dedupe", "import", "--to", "klaviyo_us", "--role", "hold", "--file", str(f), "--yes"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code != 0, args
        assert "belongs to Klaviyo account Ka6Lvr" in str(result.exception) + result.output, args
    assert writes.call_count == 0
