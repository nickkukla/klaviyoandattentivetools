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
