import csv
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import bis

API = "https://a.klaviyo.com/api"


def event(eid, pid, when, sku="SKU-1", **props):
    return {"type": "event", "id": eid,
            "attributes": {"datetime": when, "event_properties": {"SKU": sku, "VariantId": "v1", "ProductID": "p1",
                                                                  "ProductName": "Suit", "VariantName": "S", **props}},
            "relationships": {"profile": {"data": {"type": "profile", "id": pid}}}}


def profile(pid, email, *, consent="SUBSCRIBED", suppression=(), locale="en-CA", props=None, first="Ann", last="Lee"):
    return {"type": "profile", "id": pid, "attributes": {
        "email": email, "first_name": first, "last_name": last, "locale": locale, "properties": props or {},
        "subscriptions": {"email": {"marketing": {"consent": consent, "suppression": list(suppression)}}}}}


@pytest.mark.parametrize("prof,expected", [
    ({"locale": "en-CA"}, "en"), ({"locale": "fr_CA"}, "fr"), ({"locale": None}, ""),
    ({"locale": "en-CA", "properties": {"Language": "FR"}}, "fr"),
])
def test_language(prof, expected):
    assert bis.language(prof) == expected


def test_accepts_marketing_needs_subscribed_and_not_suppressed():
    ok = profile("p", "a@x.com")["attributes"]
    sup = profile("p", "a@x.com", suppression=[{"reason": "HARD_BOUNCE"}])["attributes"]
    never = profile("p", "a@x.com", consent="NEVER_SUBSCRIBED")["attributes"]
    assert (bis.accepts_marketing(ok), bis.accepts_marketing(sup), bis.accepts_marketing(never)) == (True, False, False)


def test_stoq_date_is_day_first_utc():
    assert bis.stoq_date("2026-09-24T23:30:00-04:00") == "25/09/2026"


def test_build_keeps_latest_per_email_and_sku_and_explains_the_rest():
    pairs = [
        (event("e3", "p1", "2026-09-03T00:00:00+00:00"), profile("p1", "A@x.com")["attributes"]),
        (event("e2", "p1", "2026-09-02T00:00:00+00:00"), profile("p1", "a@x.com")["attributes"]),
        (event("e1", "p1", "2026-09-01T00:00:00+00:00", sku="SKU-2"), profile("p1", "a@x.com")["attributes"]),
        (event("e0", "p2", "2026-08-01T00:00:00+00:00"), {}),
        (event("e9", "p3", "2026-08-01T00:00:00+00:00", sku=""), profile("p3", "c@x.com")["attributes"]),
    ]
    out, ref, exc = [], [], []
    counts = bis.build(iter(pairs), write=out.append, reference=ref.append, exclude=exc.append)
    assert counts == {"events": 5, "exported": 2, "excluded": 3}
    assert [(r["Email"], r["SKU"], r["Date"]) for r in out] == [("a@x.com", "SKU-1", "03/09/2026"), ("a@x.com", "SKU-2", "01/09/2026")]
    assert out[0] == {"SKU": "SKU-1", "Email": "a@x.com", "Phone": "", "Name": "Ann Lee", "Market": "", "Quantity": "",
                      "GDPR confirmed": "", "Accepts marketing": "true", "Language": "en", "Date": "03/09/2026"}
    assert [r["reason"] for r in exc] == [
        "older signup for the same email and SKU (kept 2026-09-03T00:00:00Z)", "profile has no email", "event has no SKU"]
    assert ref[0]["ca_variant_id"] == "v1" and ref[0]["ca_event_id"] == "e3"


@respx.mock
def test_bis_export_command(tmp_path, monkeypatch, klaviyo_account):
    klaviyo_account('Ka6Lvr')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_CA_API_KEY", "pk_x")
    respx.get(f"{API}/metrics/").mock(return_value=httpx.Response(200, json={"links": {"next": None}, "data": [
        {"id": "OTHER", "attributes": {"name": "Placed Order"}}, {"id": "BIS", "attributes": {"name": "Subscribed to Back in Stock"}}]}))
    included = profile("p1", "a@x.com")
    subs = included["attributes"]["subscriptions"]
    included["attributes"]["subscriptions"] = None  # as Klaviyo returns it on events
    route = respx.get(f"{API}/events/").mock(return_value=httpx.Response(200, json={
        "links": {"next": None}, "data": [event("e1", "p1", "2026-09-10T12:00:00+00:00")],
        "included": [included]}))
    lookup = respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json={
        "links": {"next": None}, "data": [{"id": "p1", "attributes": {"subscriptions": subs}}]}))
    result = CliRunner().invoke(app, ["klaviyo", "bis", "export", "--instance", "klaviyo_ca", "--since", "2026-09-01"])
    assert result.exit_code == 0, result.output
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["filter"] == ['and(equals(metric_id,"BIS"),greater-than(datetime,2026-09-01T00:00:00Z))']
    assert q["sort"] == ["-datetime"]
    d = tmp_path / "exports/klaviyo_ca/bis"
    [f] = d.glob("*.bis.csv")
    with open(f, newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == bis.STOQ_COLUMNS
        assert [dict(r) for r in reader][0]["Accepts marketing"] == "true"
    assert parse_qs(urlparse(str(lookup.calls.last.request.url)).query)["filter"] == ['any(id,["p1"])']
