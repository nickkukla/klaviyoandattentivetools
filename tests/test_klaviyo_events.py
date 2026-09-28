import csv
import json

import httpx
import respx
from typer.testing import CliRunner

from migtool.cli import app

API = "https://a.klaviyo.com/api"
EXTRA = {"id": 7024756719718, "name": "#660803LOF", "line_items": [{"title": "Swim"}]}


def page(data):
    return {"data": data, "links": {"next": None}}


def setup(tmp_path, monkeypatch, klaviyo_account, orders, header="email,order_name", events=None):
    klaviyo_account("T2aEdf")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    (tmp_path / "orders.csv").write_text(header + "\n" + "".join(",".join(o) + "\n" for o in orders))
    respx.get(f"{API}/metrics/").mock(return_value=httpx.Response(200, json=page([
        {"id": "PO1", "attributes": {"name": "Placed Order", "integration": {"name": "Shopify"}}}])))
    respx.get(f"{API}/profiles/").mock(side_effect=lambda req: httpx.Response(200, json=page(
        [{"id": "P1", "attributes": {}}] if "a%40x.com" in str(req.url) or "a@x.com" in str(req.url) else [])))
    respx.get(f"{API}/events/").mock(return_value=httpx.Response(200, json=page(events or [
        {"id": "E1", "attributes": {"datetime": "2026-09-27T15:10:44+00:00", "event_properties": {
            "$event_id": "7024756719718", "$value": 48.0, "$extra": EXTRA, "Items": ["Swim"]}}}])))
    return respx.post(f"{API}/events/").mock(return_value=httpx.Response(202))


def run(*extra):
    return CliRunner().invoke(app, ["klaviyo", "events", "resend", "--to", "klaviyo_sandbox", "--file", "orders.csv",
                                    "--metric", "Order Confirmation – Resend", "--yes", *extra])


@respx.mock
def test_resend_copies_the_order_event_exactly(tmp_path, monkeypatch, klaviyo_account):
    post = setup(tmp_path, monkeypatch, klaviyo_account, [("a@x.com", "#660803LOF")])
    result = run()
    assert result.exit_code == 0, result.output
    attrs = json.loads(post.calls.last.request.content)["data"]["attributes"]
    assert attrs["metric"]["data"]["attributes"]["name"] == "Order Confirmation – Resend"
    assert attrs["profile"]["data"]["attributes"]["email"] == "a@x.com"
    assert attrs["properties"]["$extra"] == EXTRA and attrs["properties"]["Items"] == ["Swim"]
    assert attrs["properties"]["resent_from_event_id"] == "E1" and "$event_id" not in attrs["properties"]
    assert attrs["unique_id"] == "resend-7024756719718" and attrs["value"] == 48.0


@respx.mock
def test_send_to_redirects_and_gets_its_own_unique_id(tmp_path, monkeypatch, klaviyo_account):
    post = setup(tmp_path, monkeypatch, klaviyo_account, [("a@x.com", "7024756719718")], header="email,order_id")
    assert run("--send-to", "me@x.com").exit_code == 0
    attrs = json.loads(post.calls.last.request.content)["data"]["attributes"]
    assert attrs["profile"]["data"]["attributes"]["email"] == "me@x.com"
    assert attrs["unique_id"] == "resend-7024756719718-to-me@x.com"


@respx.mock
def test_unmatched_rows_are_reported_not_sent(tmp_path, monkeypatch, klaviyo_account):
    post = setup(tmp_path, monkeypatch, klaviyo_account, [("a@x.com", "#999"), ("nobody@x.com", "#660803LOF")])
    result = run()
    assert result.exit_code != 0 and not post.called
    [errors] = (tmp_path / "exports/klaviyo_sandbox/events-resend").glob("*.errors.csv")
    reasons = [r["error"] for r in csv.DictReader(errors.open())]
    assert reasons == ["no Placed Order event with id (any) and name #999 on that profile", "no Klaviyo profile"]


def order_event(eid, oid, name):
    return {"id": eid, "attributes": {"datetime": "2026-09-27T15:00:00+00:00", "event_properties": {
        "$event_id": oid, "$extra": {"id": int(oid), "name": name}}}}


@respx.mock
def test_id_and_name_must_point_at_the_same_order(tmp_path, monkeypatch, klaviyo_account):
    """A row's order id and a stale name for another order must not pick either."""
    evs = [order_event("E222", "222", "#222"), order_event("E111", "111", "#111")]
    post = setup(tmp_path, monkeypatch, klaviyo_account, [("a@x.com", "111", "#222")],
                 header="email,order_id,order_name", events=evs)
    result = run()
    assert result.exit_code != 0 and not post.called
    assert "no Placed Order event with id 111 and name #222" in result.output


@respx.mock
def test_plan_shows_the_matched_order_and_results_are_written(tmp_path, monkeypatch, klaviyo_account):
    evs = [order_event("E222", "222", "#222"), order_event("E111", "111", "#111")]
    setup(tmp_path, monkeypatch, klaviyo_account, [("a@x.com", "111", "#111")],
          header="email,order_id,order_name", events=evs)
    result = run()
    assert result.exit_code == 0, result.output
    assert "order #111 (id 111, placed 2026-09-27T15:00:00)" in result.output and "submitted 1" in result.output
    [res] = (tmp_path / "exports/klaviyo_sandbox/events-resend").glob("*.results.csv")
    [row] = list(csv.DictReader(res.open()))
    assert (row["source_event"], row["unique_id"], row["outcome"]) == ("E111", "resend-111", "submitted")
