import csv
import functools
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.config import ConfigError, INSTANCES, Secret, check_shop, shopify_shop
from migtool.shopify import customers as sc
from migtool.shopify.client import ShopifyClient, ShopifyError

SHOP = "lof-us.myshopify.com"
GQL = f"https://{SHOP}/admin/api/2026-07/graphql.json"


def client(sleeps=None):
    return ShopifyClient(SHOP, Secret("shpat_x"), sleep=(sleeps.append if sleeps is not None else lambda _: None))


def customer(email, state="SUBSCRIBED", **kw):
    return {"id": f"gid://shopify/Customer/{abs(hash(email)) % 10**9}", "email": email, "state": "ENABLED",
            "emailMarketingConsent": {"marketingState": state, "marketingOptInLevel": "SINGLE_OPT_IN",
                                      "consentUpdatedAt": "2026-09-27T18:00:00Z"},
            "smsMarketingConsent": None, "tags": ["usa-customer"], "numberOfOrders": "2",
            "createdAt": "2026-09-27T18:00:00Z", "updatedAt": "2026-09-27T18:05:00Z",
            "defaultAddress": {"countryCodeV2": "CA"}, **kw}


def page(nodes, next_cursor=None):
    return {"data": {"customers": {"nodes": nodes, "pageInfo": {"hasNextPage": next_cursor is not None,
                                                                 "endCursor": next_cursor}}}}


@respx.mock
def test_mutations_are_refused_before_sending():
    route = respx.post(GQL)
    with pytest.raises(ValueError, match="read-only"):
        client().query('mutation { customerUpdate(input: {id: "1"}) { userErrors { message } } }')
    assert not route.called


@respx.mock
def test_throttled_queries_wait_and_retry():
    throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                 "extensions": {"cost": {"requestedQueryCost": 502,
                                         "throttleStatus": {"currentlyAvailable": 2, "restoreRate": 100}}}}
    respx.post(GQL).mock(side_effect=[httpx.Response(200, json=throttled),
                                      httpx.Response(200, json={"data": {"ok": 1}})])
    sleeps = []
    assert client(sleeps).query("query { ok }") == {"ok": 1}
    assert sleeps == [5.0]


@respx.mock
def test_other_graphql_errors_raise():
    respx.post(GQL).mock(return_value=httpx.Response(200, json={"errors": [{"message": "Access denied"}]}))
    with pytest.raises(ShopifyError, match="Access denied"):
        client().query("query { orders { nodes { id } } }")


def test_shop_config_and_guard():
    us, ca = INSTANCES["shopify_us"], INSTANCES["shopify_ca"]
    env = {"SHOPIFY_US_SHOP": "https://LOF-US.myshopify.com/", "SHOPIFY_CA_SHOP": "lof-ca.myshopify.com"}
    assert shopify_shop(us, env) == SHOP
    for bad in ("leftonfriday.com", "outside.invalid/path.myshopify.com", "user@lof-us.myshopify.com",
                "lof-us.myshopify.com:8443", "lof-us.myshopify.com/admin", "http://lof-us.myshopify.com",
                "lof-us.myshopify.com?x=1", "evil.com#.myshopify.com", ""):
        with pytest.raises(ConfigError, match="myshopify.com"):
            shopify_shop(us, {"SHOPIFY_US_SHOP": bad})
    with pytest.raises(ConfigError, match="same store"):
        shopify_shop(ca, {"SHOPIFY_US_SHOP": "https://LOF-US.myshopify.com/", "SHOPIFY_CA_SHOP": SHOP})
    with pytest.raises(ConfigError, match="belongs to Shopify store lof-ca"):
        check_shop(us, SHOP, "lof-ca.myshopify.com")


@respx.mock
def test_customers_by_email_keeps_exact_matches_across_pages():
    route = respx.post(GQL).mock(side_effect=[
        httpx.Response(200, json=page([customer("a@x.com"), customer("aa@x.com")], "c1")),
        httpx.Response(200, json=page([customer("b+tag@x.com", "UNSUBSCRIBED")])),
    ])
    found = sc.customers_by_email(client(), ["A@x.com", "b+tag@x.com"])
    assert sorted(found) == ["a@x.com", "b+tag@x.com"]
    first = json.loads(route.calls[0].request.content)
    assert first["variables"] == {"q": 'email:"a@x.com" OR email:"b+tag@x.com"', "after": None}
    assert json.loads(route.calls[1].request.content)["variables"]["after"] == "c1"


def kprofile(consent, suppression=(), **props):
    return {"id": "P1", "subscriptions": {"email": {"marketing": {
        "consent": consent, "can_receive_email_marketing": consent == "SUBSCRIBED" and not suppression,
        "suppression": [{"reason": r} for r in suppression]}}}, "properties": props}


@pytest.mark.parametrize("shop_state,k,expected", [
    ("SUBSCRIBED", kprofile("SUBSCRIBED"), "same"),
    ("SUBSCRIBED", kprofile("UNSUBSCRIBED"), "Shopify yes / Klaviyo no"),
    ("NOT_SUBSCRIBED", kprofile("SUBSCRIBED"), "Shopify no / Klaviyo yes"),
    ("NOT_SUBSCRIBED", kprofile("NEVER_SUBSCRIBED"), "same"),
    ("SUBSCRIBED", kprofile("SUBSCRIBED", ["USER_SUPPRESSED"]), "same (Klaviyo suppressed)"),
    ("SUBSCRIBED", None, "no Klaviyo profile"),
])
def test_compare(shop_state, k, expected):
    assert sc.row("a@x.com", customer("a@x.com", shop_state), k)["match"] == expected
    assert sc.row("a@x.com", None, k)["match"] == "no Shopify customer"


@respx.mock
def test_customers_command_compares_with_klaviyo(tmp_path, monkeypatch, klaviyo_account):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHOPIFY_US_SHOP", SHOP)
    monkeypatch.setenv("SHOPIFY_US_ACCESS_TOKEN", "shpat_x")
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    klaviyo_account("T2aEdf")
    shop = {"data": {"shop": {"name": "LOF US", "myshopifyDomain": SHOP},
                     "currentAppInstallation": {"accessScopes": [{"handle": "read_customers"}]}}}
    respx.post(GQL).mock(side_effect=[httpx.Response(200, json=shop),
                                      httpx.Response(200, json=page([customer("a@x.com", "SUBSCRIBED")]))])
    respx.get("https://a.klaviyo.com/api/profiles/").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "P1", "attributes": {"email": "a@x.com", **{k: v for k, v in kprofile(
            "UNSUBSCRIBED", migration_hold=True, **{"Accepts Marketing": True}).items() if k != "id"}}}],
        "links": {"next": None}}))
    result = CliRunner().invoke(app, ["shopify", "customers", "--instance", "shopify_us", "--email", "a@x.com",
                                      "--email", "missing@x.com", "--compare", "klaviyo_sandbox"])
    assert result.exit_code == 0, result.output
    assert "Shopify yes / Klaviyo no: 1" in result.output and "no Shopify customer: 1" in result.output
    [out] = (tmp_path / "exports/shopify_us/customers").glob("*.csv")
    rows = {r["email"]: r for r in csv.DictReader(out.open())}
    assert rows["a@x.com"]["klaviyo_consent"] == "UNSUBSCRIBED" and rows["a@x.com"]["klaviyo_migration_hold"] == "true"


@respx.mock
def test_whoami_stops_on_the_wrong_store(monkeypatch):
    monkeypatch.setenv("SHOPIFY_US_SHOP", SHOP)
    monkeypatch.setenv("SHOPIFY_US_ACCESS_TOKEN", "shpat_x")
    respx.post(GQL).mock(return_value=httpx.Response(200, json={"data": {
        "shop": {"name": "LOF CA", "myshopifyDomain": "lof-ca.myshopify.com"},
        "currentAppInstallation": {"accessScopes": []}}}))
    result = CliRunner().invoke(app, ["shopify", "whoami", "--instance", "shopify_us"])
    assert result.exit_code != 0 and "belongs to Shopify store lof-ca" in str(result.exception)


OP = "gid://shopify/BulkOperation/1"
RESULT = "https://storage.example/result.jsonl"


def op(status, count="2", **kw):
    return {"id": OP, "status": status, "type": "QUERY", "objectCount": count, "url": RESULT if status == "COMPLETED" else None,
            "query": sc.BULK_QUERY, "createdAt": "2099-01-01T00:00:00Z", **kw}


def started():
    return httpx.Response(200, json={"data": {"bulkOperationRunQuery": {
        "bulkOperation": {"id": OP, "status": "CREATED"}, "userErrors": []}}})


def node(o):
    return httpx.Response(200, json={"data": {"node": o}})


def listing(*ops):
    return httpx.Response(200, json={"data": {"bulkOperations": {"nodes": list(ops)}}})


@respx.mock
def test_start_bulk_export_sends_only_the_bulk_mutation():
    route = respx.post(GQL).mock(side_effect=[started()])
    assert client().start_bulk_export(sc.BULK_QUERY) == OP
    [call] = route.calls
    body = json.loads(call.request.content)
    assert "bulkOperationRunQuery" in body["query"] and body["variables"]["q"] == sc.BULK_QUERY


def test_bulk_export_refuses_a_query_containing_a_mutation():
    with pytest.raises(ValueError):
        client().start_bulk_export("mutation { customerDelete(input: {id: \"1\"}) { deletedCustomerId } }")


@respx.mock
def test_a_lost_start_is_looked_up_not_retried():
    route = respx.post(GQL).mock(side_effect=[httpx.Response(504), listing(op("RUNNING"))])
    assert client().start_bulk_export(sc.BULK_QUERY) == OP
    assert sum("bulkOperationRunQuery" in json.loads(c.request.content)["query"] for c in route.calls) == 1


@respx.mock
def test_wait_bulk_polls_until_complete():
    respx.post(GQL).mock(side_effect=[node(op("RUNNING", "1")), node(op("COMPLETED"))])
    assert client().wait_bulk(OP, poll_seconds=0)["url"] == RESULT


def export_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHOPIFY_US_SHOP", SHOP)
    monkeypatch.setenv("SHOPIFY_US_ACCESS_TOKEN", "shpat_x")
    monkeypatch.setattr(ShopifyClient, "wait_bulk", functools.partialmethod(ShopifyClient.wait_bulk, poll_seconds=0))
    return httpx.Response(200, json={"data": {"shop": {"name": "LOF US", "myshopifyDomain": SHOP},
                                              "currentAppInstallation": {"accessScopes": []}}})


def jsonl(*pairs):
    return httpx.Response(200, text="\n".join(json.dumps(customer(e, s)) for e, s in pairs) + "\n")


@respx.mock
def test_customers_export_starts_saves_and_writes(tmp_path, monkeypatch):
    shop = export_env(tmp_path, monkeypatch)
    route = respx.post(GQL).mock(side_effect=[shop, listing(), started(), node(op("COMPLETED"))])
    respx.get(RESULT).mock(return_value=jsonl(("A@x.com", "SUBSCRIBED"), ("b@x.com", "NOT_SUBSCRIBED")))
    result = CliRunner().invoke(app, ["shopify", "customers-export", "--instance", "shopify_us"])
    assert result.exit_code == 0, result.output
    [out] = (tmp_path / "exports/shopify_us/customers-export").glob("*.csv")
    assert [(r["email"], r["email_marketing"]) for r in csv.DictReader(out.open())] == [
        ("a@x.com", "SUBSCRIBED"), ("b@x.com", "NOT_SUBSCRIBED")]
    assert not (tmp_path / "state/shopify_us/shopify-customers-bulk.checkpoint.json").exists()


@respx.mock
def test_customers_export_reuses_a_recent_export(tmp_path, monkeypatch):
    shop = export_env(tmp_path, monkeypatch)
    route = respx.post(GQL).mock(side_effect=[shop, listing(op("COMPLETED")), node(op("COMPLETED"))])
    respx.get(RESULT).mock(return_value=jsonl(("a@x.com", "SUBSCRIBED"), ("b@x.com", "SUBSCRIBED")))
    result = CliRunner().invoke(app, ["shopify", "customers-export", "--instance", "shopify_us"])
    assert result.exit_code == 0, result.output and "Reusing bulk export" in result.output
    assert not any("bulkOperationRunQuery" in json.loads(c.request.content)["query"] for c in route.calls)


@respx.mock
def test_an_incomplete_download_stays_a_part_file(tmp_path, monkeypatch):
    shop = export_env(tmp_path, monkeypatch)
    respx.post(GQL).mock(side_effect=[shop, listing(), started(), node(op("COMPLETED", "3"))])
    respx.get(RESULT).mock(return_value=jsonl(("a@x.com", "SUBSCRIBED")))
    result = CliRunner().invoke(app, ["shopify", "customers-export", "--instance", "shopify_us"])
    assert result.exit_code != 0 and "Downloaded 1 customers but the export has 3" in str(result.exception)
    d = tmp_path / "exports/shopify_us/customers-export"
    assert not list(d.glob("*.csv")) and list(d.glob("*.csv.part"))
    # the saved operation lets a re-run continue it
    assert json.loads((tmp_path / "state/shopify_us/shopify-customers-bulk.checkpoint.json").read_text())["id"] == OP
