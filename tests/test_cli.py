from typer.testing import CliRunner

from migtool.cli import app


def test_instances_shows_status_not_values(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("KLAVIYO_US_API_KEY", "KLAVIYO_CA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KLAVIYO_US_API_KEY", "pk_live_topsecret")
    result = CliRunner().invoke(app, ["instances"])
    assert result.exit_code == 0
    assert "topsecret" not in result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()}
    assert lines["klaviyo_us"].endswith("set")
    assert lines["klaviyo_ca"].endswith("NOT SET")


def test_sub_apps_exist():
    result = CliRunner().invoke(app, ["--help"])
    for name in ("klaviyo", "instances"):
        assert name in result.output
