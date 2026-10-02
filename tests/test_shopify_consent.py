import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import dedupe
from migtool.shopify import consent
from migtool.shopify import customers as sc
from migtool.shopify.client import ShopifyClient

from test_shopify import GQL, SHOP, client

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
K_COLUMNS = ["id", "email", "consent", "consent_timestamp", "method", "suppressions", "suppression_timestamp",
             "properties.migrated_from", "properties.ca_consent_timestamp", "properties.ca_suppression_timestamp#text"]


def kprofile(email, consent_state="SUBSCRIBED", ts="2025-05-01T10:00:00.123+00:00", *, sup=(), migrated="",
             ca_ts="", ca_sup_ts="", method="API", pid=None):
    return {"id": pid or f"K-{email}", "email": email, "consent": consent_state, "consent_timestamp": ts,
            "method": method, "suppressions": json.dumps([{"reason": r, "timestamp": t} for r, t in sup]),
            "suppression_timestamp": sup[0][1] if sup else "", "properties.migrated_from": migrated,
            "properties.ca_consent_timestamp": ca_ts, "properties.ca_suppression_timestamp#text": ca_sup_ts}


def scustomer(email, state, date="2024-01-01T00:00:00Z", cid=None):
    return {"customer_id": cid or str(abs(hash(email)) % 10**9), "email": email, "email_marketing": state,
            "email_opt_in_level": "SINGLE_OPT_IN", "email_consent_updated": date}


def write(path, columns, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def exports(tmp_path, kprofiles, scustomers):
    return (write(tmp_path / "k.csv", K_COLUMNS, kprofiles),
            write(tmp_path / "s.csv", sc.EXPORT_COLUMNS, scustomers))


# --- Mapping ---------------------------------------------------------------------

@pytest.mark.parametrize("k,s,expected", [
    ("subscribed", "SUBSCRIBED", "match"), ("subscribed", "NOT_SUBSCRIBED", "write"), ("subscribed", "UNSUBSCRIBED", "write"),
    ("unsubscribed", "UNSUBSCRIBED", "match"), ("unsubscribed", "SUBSCRIBED", "write"), ("unsubscribed", "NOT_SUBSCRIBED", "write"),
    ("suppressed", "UNSUBSCRIBED", "match"), ("suppressed", "SUBSCRIBED", "write"), ("suppressed", "NOT_SUBSCRIBED", "write"),
    ("never", "NOT_SUBSCRIBED", "match"), ("never", "SUBSCRIBED", "d14"), ("never", "UNSUBSCRIBED", "d12"),
    ("never", "INVALID", "excluded"), ("subscribed", "REDACTED", "excluded"),
])
def test_action_follows_the_agreed_mapping(k, s, expected):
    assert consent.action(k, s) == expected


def test_klaviyo_state_and_original_dates():
    sup = consent.klaviyo_consent(kprofile("a@x.com", "SUBSCRIBED", sup=[("HARD_BOUNCE", "2025-02-01T00:00:00Z")],
                                           migrated="ca", ca_sup_ts="2024-03-03T00:00:00Z"))
    assert (sup.state, sup.date) == ("suppressed", "2024-03-03T00:00:00Z")  # migrated: CA suppression date
    unsub_only = consent.klaviyo_consent(kprofile("b@x.com", "NEVER_SUBSCRIBED", "", sup=[("UNSUBSCRIBE", "2025-01-01T00:00:00Z")]))
    assert unsub_only.state == "unsubscribed"
    migrated = consent.klaviyo_consent(kprofile("c@x.com", ts="2026-09-26T05:00:00Z", migrated="ca", ca_ts="2022-07-01T00:00:00Z"))
    assert migrated.date == "2022-07-01T00:00:00Z"  # D5: the original CA date, not the import time
    us = consent.klaviyo_consent(kprofile("d@x.com", ts="2025-05-01T10:00:00.123+00:00"))
    assert (us.state, us.date) == ("subscribed", "2025-05-01T10:00:00.123+00:00")


def test_plan_writes_d14_excluded_and_dates(tmp_path):
    k, s = exports(tmp_path, [
        kprofile("sub@x.com"),                                                     # → SUBSCRIBED
        kprofile("unsub@x.com", "UNSUBSCRIBED", "2025-03-01T00:00:00Z"),            # → UNSUBSCRIBED (D10)
        kprofile("same@x.com"),                                                    # match
        kprofile("never@x.com", "NEVER_SUBSCRIBED", ""),                           # D14
        kprofile("d12@x.com", "NEVER_SUBSCRIBED", ""),                             # D12
        kprofile("future@x.com", ts="2027-01-01T00:00:00Z"),                       # clamped
        kprofile("noshop@x.com"),                                                  # no Shopify customer
        kprofile("newer@x.com", "UNSUBSCRIBED", "2024-01-01T00:00:00Z"),           # Shopify newer (D11)
    ], [
        scustomer("sub@x.com", "NOT_SUBSCRIBED", ""), scustomer("unsub@x.com", "NOT_SUBSCRIBED", ""),
        scustomer("same@x.com", "SUBSCRIBED"), scustomer("never@x.com", "SUBSCRIBED", "2026-03-02T02:24:25Z"),
        scustomer("d12@x.com", "UNSUBSCRIBED"), scustomer("future@x.com", "NOT_SUBSCRIBED", ""),
        scustomer("newer@x.com", "SUBSCRIBED", "2026-05-05T00:00:00Z"),
    ])
    p = consent.plan(k, s, now=NOW)
    by = {w["email"]: w for w in p.writes}
    assert set(by) == {"sub@x.com", "unsub@x.com", "future@x.com", "newer@x.com"}
    assert {w["target_state"] for w in p.writes} <= {"SUBSCRIBED", "UNSUBSCRIBED"}  # never NOT_SUBSCRIBED
    assert by["sub@x.com"]["target_date"] == "2025-05-01T10:00:00Z"
    assert by["unsub@x.com"]["target_state"] == "UNSUBSCRIBED"
    assert by["future@x.com"]["target_date"] == "2026-10-02T20:00:00Z" and p.clamped == 1
    assert by["newer@x.com"]["shopify_newer"] is True and p.shopify_newer == 1
    assert p.d14 == [{"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2026-03-02T02:24:25Z",
                      "email": "never@x.com", "shopify_customer_id": by_id(s, "never@x.com")}]
    assert [e["email"] for e in p.excluded] == ["d12@x.com"]
    assert (p.profiles, p.matched) == (8, 7)


def by_id(path, email):
    return next(r["customer_id"] for r in consent.read_csv(path) if r["email"] == email)


def test_plan_can_be_limited_to_emails_and_refuses_duplicate_shopify_emails(tmp_path):
    k, s = exports(tmp_path, [kprofile("a@x.com"), kprofile("b@x.com")],
                   [scustomer("a@x.com", "NOT_SUBSCRIBED"), scustomer("b@x.com", "NOT_SUBSCRIBED")])
    assert [w["email"] for w in consent.plan(k, s, now=NOW, only={"b@x.com"}).writes] == ["b@x.com"]
    write(s, sc.EXPORT_COLUMNS, [scustomer("a@x.com", "SUBSCRIBED", cid="1"), scustomer("A@x.com", "SUBSCRIBED", cid="2")])
    with pytest.raises(ValueError, match="more than one Shopify customer"):
        consent.plan(k, s, now=NOW)


# --- Client ------------------------------------------------------------------------

def test_update_refuses_not_subscribed_before_sending():
    with pytest.raises(ValueError, match="SUBSCRIBED or UNSUBSCRIBED"):
        client().update_email_consent("1", "NOT_SUBSCRIBED", None)


@respx.mock
def test_update_sends_only_the_consent_mutation():
    route = respx.post(GQL).mock(return_value=httpx.Response(200, json={"data": {"customerEmailMarketingConsentUpdate": {
        "customer": {"id": "gid://shopify/Customer/1"}, "userErrors": []}}}))
    client().update_email_consent("1", "SUBSCRIBED", "2024-05-01T00:00:00Z")
    body = json.loads(route.calls.last.request.content)
    assert "customerEmailMarketingConsentUpdate" in body["query"]
    assert body["variables"] == {"input": {"customerId": "gid://shopify/Customer/1", "emailMarketingConsent": {
        "marketingState": "SUBSCRIBED", "marketingOptInLevel": "SINGLE_OPT_IN", "consentUpdatedAt": "2024-05-01T00:00:00Z"}}}


def test_query_still_refuses_mutations():
    with pytest.raises(ValueError, match="read-only"):
        client().query("mutation { customerDelete(input: {id: \"1\"}) { deletedCustomerId } }")


# --- consent-sync ---------------------------------------------------------------------

class FakeStore:
    """Shopify customers' email consent, answering the queries consent-sync sends."""

    def __init__(self, states, *, scopes=("read_customers", "write_customers"), refuse=(), lose=(), domain=SHOP):
        self.states = dict(states)  # numeric id → state
        self.scopes, self.refuse, self.lose, self.domain = scopes, set(refuse), set(lose), domain
        self.mutations = []

    def __call__(self, request):
        body = json.loads(request.content)
        q, v = body["query"], body.get("variables") or {}
        if "shop {" in q:
            return httpx.Response(200, json={"data": {"shop": {"name": "LOF US", "myshopifyDomain": self.domain},
                                                      "currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]}}})
        if "nodes(ids" in q:
            nodes = []
            for gid in v["ids"]:
                cid = gid.rsplit("/", 1)[-1]
                nodes.append(None if cid not in self.states else {"id": gid, "email": f"{cid}@x.com", "emailMarketingConsent": {
                    "marketingState": self.states[cid], "marketingOptInLevel": "SINGLE_OPT_IN", "consentUpdatedAt": None}})
            return httpx.Response(200, json={"data": {"nodes": nodes}})
        assert "customerEmailMarketingConsentUpdate" in q, q
        cid = v["input"]["customerId"].rsplit("/", 1)[-1]
        self.mutations.append((cid, v["input"]["emailMarketingConsent"]))
        if cid in self.refuse:
            return httpx.Response(200, json={"data": {"customerEmailMarketingConsentUpdate": {
                "customer": None, "userErrors": [{"field": ["input"], "message": "nope", "code": "INVALID"}]}}})
        self.states[cid] = v["input"]["emailMarketingConsent"]["marketingState"]
        if cid in self.lose:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"customerEmailMarketingConsentUpdate": {
            "customer": {"id": v["input"]["customerId"], "emailMarketingConsent": v["input"]["emailMarketingConsent"]},
            "userErrors": []}}})


def plan_file(tmp_path, rows):
    return write(tmp_path / "x.plan.csv", consent.PLAN_COLUMNS, [
        {"shopify_customer_id": cid, "email": f"{cid}@x.com", "klaviyo_profile_id": f"K{cid}", "klaviyo_state": "s",
         "shopify_state": "", "target_state": t, "target_date": d, "shopify_date": "", "shopify_newer": False}
        for cid, t, d in rows])


def sync_env(tmp_path, monkeypatch, store):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHOPIFY_US_SHOP", SHOP)
    monkeypatch.setenv("SHOPIFY_US_ACCESS_TOKEN", "shpat_x")
    respx.post(GQL).mock(side_effect=store)


def sync(*args):
    return CliRunner().invoke(app, ["shopify", "consent-sync", "--to", "shopify_us", "--yes", *args])


def results(tmp_path):
    out = {}
    for path in sorted((tmp_path / "exports/shopify_us/consent-sync").glob("*.results.csv")):
        out.update({r["shopify_customer_id"]: r for r in consent.read_csv(path)})
    return out


@respx.mock
def test_sync_writes_skips_refuses_and_reads_back_lost_responses(tmp_path, monkeypatch):
    store = FakeStore({"1": "NOT_SUBSCRIBED", "2": "SUBSCRIBED", "3": "SUBSCRIBED", "4": "SUBSCRIBED"},
                      refuse={"3"}, lose={"4"})
    sync_env(tmp_path, monkeypatch, store)
    p = plan_file(tmp_path, [("1", "SUBSCRIBED", "2024-05-01T00:00:00Z"), ("2", "SUBSCRIBED", "2024-05-01T00:00:00Z"),
                             ("3", "UNSUBSCRIBED", "2025-01-01T00:00:00Z"), ("4", "UNSUBSCRIBED", "2025-01-01T00:00:00Z"),
                             ("5", "UNSUBSCRIBED", "2025-01-01T00:00:00Z")])
    result = sync("--plan", str(p))
    assert result.exit_code == 1, result.output  # a refusal and a missing customer
    r = results(tmp_path)
    assert {k: v["outcome"] for k, v in r.items()} == {"1": "written", "2": "skipped", "3": "refused",
                                                       "4": "written", "5": "not found"}
    assert "confirmed by read-back" in r["4"]["detail"] and r["3"]["detail"] == "nope"
    assert [m[0] for m in store.mutations] == ["1", "3", "4"]  # 2 was already SUBSCRIBED: nothing sent
    assert store.mutations[0][1] == {"marketingState": "SUBSCRIBED", "marketingOptInLevel": "SINGLE_OPT_IN",
                                     "consentUpdatedAt": "2024-05-01T00:00:00Z"}


@respx.mock
def test_sync_canary_then_resume_skips_done_customers(tmp_path, monkeypatch):
    store = FakeStore({str(i): "NOT_SUBSCRIBED" for i in range(1, 7)})
    sync_env(tmp_path, monkeypatch, store)
    p = plan_file(tmp_path, [(str(i), "SUBSCRIBED" if i % 2 else "UNSUBSCRIBED", "2024-05-01T00:00:00Z") for i in range(1, 7)])
    assert sync("--plan", str(p), "--target", "SUBSCRIBED", "--limit", "1").exit_code == 0
    assert [m[0] for m in store.mutations] == ["1"]
    again = sync("--plan", str(p))
    assert again.exit_code != 0 and "Use --resume" in str(again.exception or again.output)
    assert sync("--plan", str(p), "--resume").exit_code == 0
    assert sorted(m[0] for m in store.mutations) == ["1", "2", "3", "4", "5", "6"]  # 1 not written twice
    assert all(v["outcome"] == "written" for v in results(tmp_path).values())


@respx.mock
def test_sync_needs_write_customers_and_the_right_store(tmp_path, monkeypatch):
    p = plan_file(tmp_path, [("1", "SUBSCRIBED", "")])
    sync_env(tmp_path, monkeypatch, FakeStore({"1": "NOT_SUBSCRIBED"}, scopes=("read_customers",)))
    result = sync("--plan", str(p))
    assert result.exit_code != 0 and "write_customers" in str(result.exception)
    respx.reset()
    respx.post(GQL).mock(side_effect=FakeStore({"1": "NOT_SUBSCRIBED"}, domain="lof-ca.myshopify.com"))
    result = sync("--plan", str(p))
    assert result.exit_code != 0 and "lof-ca" in str(result.exception)


def test_sync_refuses_a_plan_with_not_subscribed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = plan_file(tmp_path, [("1", "NOT_SUBSCRIBED", "")])
    result = sync("--plan", str(p))
    assert result.exit_code != 0 and "can't take" in str(result.exception)


# --- consent-validate ---------------------------------------------------------------------

def test_validate_states_dates_d12_d14_and_klaviyo_changes(tmp_path):
    k, s = exports(tmp_path, [
        kprofile("ok@x.com"),
        kprofile("wrong@x.com", "UNSUBSCRIBED"),
        kprofile("d12@x.com", "NEVER_SUBSCRIBED", ""),
        kprofile("dated@x.com"),
        kprofile("d14@x.com", ts="2026-03-02T02:24:25+00:00", pid="KD14"),
    ], [
        scustomer("ok@x.com", "SUBSCRIBED", "2025-05-01T10:00:00Z", cid="1"),
        scustomer("wrong@x.com", "SUBSCRIBED", cid="2"),
        scustomer("d12@x.com", "UNSUBSCRIBED", cid="3"),
        scustomer("dated@x.com", "SUBSCRIBED", "2026-10-02T21:00:00Z", cid="4"),
        scustomer("d14@x.com", "SUBSCRIBED", "2026-03-02T02:24:25Z", cid="5"),
    ])
    res = write(tmp_path / "r.csv", ["shopify_customer_id", "target_state", "target_date", "outcome"], [
        {"shopify_customer_id": "1", "target_state": "SUBSCRIBED", "target_date": "2025-05-01T10:00:00Z", "outcome": "written"},
        {"shopify_customer_id": "4", "target_state": "SUBSCRIBED", "target_date": "2025-05-01T10:00:00Z", "outcome": "written"}])
    before = write(tmp_path / "before.csv", K_COLUMNS, [kprofile("ok@x.com"), kprofile("d14@x.com", "NEVER_SUBSCRIBED", "", pid="KD14"),
                                                       kprofile("wrong@x.com", "UNSUBSCRIBED", method="PREFERENCE_PAGE")])
    v = consent.validate(k, s, written=consent.written_targets([res]), d14_emails={"d14@x.com"}, klaviyo_before=before)
    problems = {m["email"]: m["problem"] for m in v.mismatches}
    assert set(problems) == {"wrong@x.com", "dated@x.com"}
    assert "Klaviyo unsubscribed, Shopify SUBSCRIBED" in problems["wrong@x.com"]
    assert "the run wrote 2025-05-01T10:00:00Z" in problems["dated@x.com"]  # P8b: dates checked where written
    assert (v.counts["ok"], v.counts["d12"]) == (3, 1)  # ok, d12, d14
    assert v.counts["klaviyo_changed_d14"] == 2 and v.counts["klaviyo_changed"] == 1  # d14 state+date; wrong's method
    assert {c["field"] for c in v.klaviyo_changes if c["email"] == "wrong@x.com"} == {"method"}


@respx.mock
def test_plan_and_validate_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    k, s = exports(tmp_path, [kprofile("a@x.com"), kprofile("n@x.com", "NEVER_SUBSCRIBED", "")],
                   [scustomer("a@x.com", "NOT_SUBSCRIBED", "", cid="1"), scustomer("n@x.com", "SUBSCRIBED", cid="2")])
    result = CliRunner().invoke(app, ["shopify", "consent-plan", "--klaviyo", str(k), "--shopify", str(s)])
    assert result.exit_code == 0, result.output
    assert "Shopify writes: 1 (1 SUBSCRIBED, 0 UNSUBSCRIBED)" in result.output and "Klaviyo writes (D14): 1" in result.output
    run = tmp_path / "exports/shopify_us/consent-plan"
    assert len(list(run.glob("*.plan.csv"))) == 1 and len(list(run.glob("*.klaviyo_d14.csv"))) == 1
    result = CliRunner().invoke(app, ["shopify", "consent-validate", "--klaviyo", str(k), "--shopify", str(s)])
    assert result.exit_code == 1 and "mismatched 2" in result.output  # nothing synced yet


# --- D14 Klaviyo role ------------------------------------------------------------------

def test_consent_role_updates_existing_profiles_only_with_shopify_date_and_no_tags():
    role = dedupe.ROLES["consent"]
    assert (role.creates, role.join_list, role.consent, role.tags) == (False, False, True, False)
    rows = [{"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2026-03-02T02:24:25Z",
             "email": "d14@x.com", "shopify_customer_id": "5"},
            {"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2026-03-02T02:24:25Z",
             "email": "gone@x.com", "shopify_customer_id": "6"}]
    p = dedupe.plan(role, rows, {}, "RUN", lambda e, ph: {"d14@x.com"})
    assert [a["email"] for a in p.payloads] == ["d14@x.com"]
    assert "migrated_from" not in (p.payloads[0].get("properties") or {})
    assert p.skipped == [("gone@x.com", "no existing profile in the destination (update only)")]


def test_consent_role_subscribes_with_its_own_source():
    from test_klaviyo_dedupe import FakeImporter
    imp = FakeImporter()
    role = dedupe.ROLES["consent"]
    p = dedupe.plan(role, [{"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2026-03-02T02:24:25Z",
                            "email": "d14@x.com"}], {}, "RUN", lambda e, ph: {"d14@x.com"})
    dedupe.run(imp, p, join_list=None, subscribe_list="Xz4KGg")
    assert ("subscribe", [("d14@x.com", "2026-03-02T02:24:25Z")], "Xz4KGg") in imp.calls
    assert imp.source == "Shopify email consent (consent sync)"


def test_consent_role_check_compares_the_consent_date():
    role = dedupe.ROLES["consent"]
    row = {"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2026-03-02T02:24:25Z", "email": "d14@x.com"}
    stored = {"_id": "P1", "subscriptions": {"email": {"marketing": {"consent": "SUBSCRIBED", "consent_timestamp": "2026-10-02T21:00:00Z"}}}}
    found = dedupe.problems(role, row, stored, types={}, us_profile=False, join=None, subscribe={"P1"})
    assert any("consent_timestamp" in f for f in found)
