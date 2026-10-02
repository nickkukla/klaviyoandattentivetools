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
    # D16: Shopify ignores a change dated before its own date, so this one is dated at the sync time.
    assert (by["newer@x.com"]["date_rule"], by["newer@x.com"]["target_date"]) == ("sync time", "")
    assert by["newer@x.com"]["klaviyo_date"] == "2024-01-01T00:00:00Z"
    assert by["sub@x.com"]["date_rule"] == "original" and p.sync_time == 1
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

    def __init__(self, states, *, scopes=("read_customers", "write_customers"), refuse=(), lose=(), domain=SHOP,
                 dates=None):
        self.states = dict(states)  # numeric id → state
        self.dates = dict(dates or {})  # numeric id → consentUpdatedAt
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
                    "marketingState": self.states[cid], "marketingOptInLevel": "SINGLE_OPT_IN",
                    "consentUpdatedAt": self.dates.get(cid)}})
            return httpx.Response(200, json={"data": {"nodes": nodes}})
        assert "customerEmailMarketingConsentUpdate" in q, q
        cid = v["input"]["customerId"].rsplit("/", 1)[-1]
        self.mutations.append((cid, v["input"]["emailMarketingConsent"]))
        if cid in self.refuse:
            return httpx.Response(200, json={"data": {"customerEmailMarketingConsentUpdate": {
                "customer": None, "userErrors": [{"field": ["input"], "message": "nope", "code": "INVALID"}]}}})
        sent = v["input"]["emailMarketingConsent"]
        at = sent.get("consentUpdatedAt")
        # Like Shopify: a change dated before the current consent date is ignored, without an error.
        if not (at and self.dates.get(cid) and at < self.dates[cid]):
            self.states[cid], self.dates[cid] = sent["marketingState"], at
        if cid in self.lose:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"customerEmailMarketingConsentUpdate": {
            "customer": {"id": v["input"]["customerId"], "emailMarketingConsent": {
                "marketingState": self.states[cid], "marketingOptInLevel": "SINGLE_OPT_IN",
                "consentUpdatedAt": self.dates.get(cid)}},
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
    assert "read back: stored UNSUBSCRIBED" in r["4"]["detail"] and r["3"]["detail"] == "nope"
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

RES_COLUMNS = ["shopify_customer_id", "email", "target_state", "target_date", "outcome"]


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
    res = write(tmp_path / "r.csv", RES_COLUMNS, [
        {"shopify_customer_id": "1", "email": "ok@x.com", "target_state": "SUBSCRIBED", "target_date": "2025-05-01T10:00:00Z", "outcome": "written"},
        {"shopify_customer_id": "4", "email": "dated@x.com", "target_state": "SUBSCRIBED", "target_date": "2025-05-01T10:00:00Z", "outcome": "written"}])
    before = write(tmp_path / "before.csv", K_COLUMNS, [kprofile("ok@x.com"), kprofile("d14@x.com", "NEVER_SUBSCRIBED", "", pid="KD14"),
                                                       kprofile("wrong@x.com", "UNSUBSCRIBED", method="PREFERENCE_PAGE")])
    v = consent.validate(k, s, written=consent.written_targets([res]), d14_emails={"d14@x.com"}, klaviyo_before=before)
    problems = {m["email"]: m["problem"] for m in v.mismatches}
    assert set(problems) == {"wrong@x.com", "dated@x.com"}  # dated appears via the written-customer check
    assert "Klaviyo unsubscribed, Shopify SUBSCRIBED" in problems["wrong@x.com"]
    assert "the run wrote 2025-05-01T10:00:00Z" in problems["dated@x.com"]  # P8b: dates checked where written
    assert (v.counts["ok"], v.counts["d12"]) == (4, 1)  # ok, d12, d14, dated (its state matches)
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


# --- Review 105 regressions ------------------------------------------------------------

def test_plan_refuses_exports_without_the_needed_columns_or_values(tmp_path):
    k = write(tmp_path / "k.csv", ["id", "email"], [{"id": "K1", "email": "a@x.com"}])
    s = write(tmp_path / "s.csv", sc.EXPORT_COLUMNS, [scustomer("a@x.com", "SUBSCRIBED")])
    with pytest.raises(ValueError, match="missing column"):  # not read as "never subscribed" → D14
        consent.plan(k, s, now=NOW)
    k, s = exports(tmp_path, [kprofile("a@x.com", "")], [scustomer("a@x.com", "SUBSCRIBED")])
    with pytest.raises(ValueError, match="has consent ''"):
        consent.plan(k, s, now=NOW)
    k, s = exports(tmp_path, [kprofile("a@x.com")], [scustomer("a@x.com", "")])
    with pytest.raises(ValueError, match="email state"):
        consent.plan(k, s, now=NOW)
    bad = kprofile("a@x.com"); bad["suppressions"] = "{not json"
    k, s = exports(tmp_path, [bad], [scustomer("a@x.com", "SUBSCRIBED")])
    with pytest.raises(ValueError, match="valid JSON"):
        consent.plan(k, s, now=NOW)


def test_unsubscribe_suppression_date_is_used_when_consent_has_none():
    k = consent.klaviyo_consent(kprofile("a@x.com", "NEVER_SUBSCRIBED", "", sup=[("UNSUBSCRIBE", "2025-06-01T00:00:00Z")]))
    assert (k.state, k.date) == ("unsubscribed", "2025-06-01T00:00:00Z")


def test_validate_requires_written_and_d14_customers_to_be_present_and_subscribed(tmp_path):
    k, s = exports(tmp_path, [kprofile("d14@x.com", "NEVER_SUBSCRIBED", ""), kprofile("moved@x.com")],
                   [scustomer("d14@x.com", "NOT_SUBSCRIBED", "", cid="5"), scustomer("new@x.com", "SUBSCRIBED", cid="7")])
    res = write(tmp_path / "r.csv", RES_COLUMNS, [
        {"shopify_customer_id": "6", "email": "gone@x.com", "target_state": "SUBSCRIBED", "target_date": "", "outcome": "written"},
        {"shopify_customer_id": "7", "email": "moved@x.com", "target_state": "SUBSCRIBED", "target_date": "", "outcome": "written"}])
    v = consent.validate(k, s, written=consent.written_targets([res]), d14_emails={"d14@x.com", "lost@x.com"})
    problems = {m["email"]: m["problem"] for m in v.mismatches}
    assert "expected subscribed on both sides" in problems["d14@x.com"]  # never/NOT_SUBSCRIBED isn't a D14 pass
    assert "not in the post-run Shopify export" in problems["gone@x.com"]
    assert "email is now new@x.com" in problems["moved@x.com"]
    assert "no matching Klaviyo profile" in problems["lost@x.com"]


def test_klaviyo_profiles_gone_since_the_backup_are_listed(tmp_path):
    k, s = exports(tmp_path, [kprofile("a@x.com")], [scustomer("a@x.com", "SUBSCRIBED")])
    before = write(tmp_path / "before.csv", K_COLUMNS, [kprofile("a@x.com"), kprofile("merged@x.com")])
    v = consent.validate(k, s, klaviyo_before=before)
    assert v.counts["klaviyo_missing"] == 1 and v.klaviyo_changes[0]["email"] == "merged@x.com"


class EmailChangedStore(FakeStore):
    def __call__(self, request):
        response = super().__call__(request)
        body = json.loads(request.content)
        if "nodes(ids" in body["query"]:
            data = response.json()
            data["data"]["nodes"][0]["email"] = "someone-else@x.com"
            return httpx.Response(200, json=data)
        return response


@respx.mock
def test_sync_never_writes_a_customer_whose_email_changed(tmp_path, monkeypatch):
    store = EmailChangedStore({"1": "NOT_SUBSCRIBED"})
    sync_env(tmp_path, monkeypatch, store)
    result = sync("--plan", str(plan_file(tmp_path, [("1", "SUBSCRIBED", "2024-05-01T00:00:00Z")])))
    assert result.exit_code == 1 and not store.mutations
    assert results(tmp_path)["1"]["outcome"] == "identity conflict"


@respx.mock
def test_resume_refuses_an_edited_plan(tmp_path, monkeypatch):
    sync_env(tmp_path, monkeypatch, FakeStore({"1": "NOT_SUBSCRIBED", "2": "NOT_SUBSCRIBED"}))
    p = plan_file(tmp_path, [("1", "SUBSCRIBED", ""), ("2", "SUBSCRIBED", "")])
    assert sync("--plan", str(p), "--limit", "1").exit_code == 0
    plan_file(tmp_path, [("1", "UNSUBSCRIBED", ""), ("2", "SUBSCRIBED", "")])  # same path, new content
    result = sync("--plan", str(p), "--resume")
    assert result.exit_code != 0 and "different now" in str(result.exception)


@respx.mock
def test_unresolved_customers_stay_reported_until_retried(tmp_path, monkeypatch):
    store = FakeStore({"1": "NOT_SUBSCRIBED", "2": "NOT_SUBSCRIBED"}, refuse={"1"})
    sync_env(tmp_path, monkeypatch, store)
    p = plan_file(tmp_path, [("1", "SUBSCRIBED", ""), ("2", "SUBSCRIBED", "")])
    assert sync("--plan", str(p), "--limit", "1").exit_code == 1  # 1 refused
    store.refuse.clear()
    again = sync("--plan", str(p), "--resume")  # writes 2; 1 is still unresolved and not retried
    assert again.exit_code == 1 and "Unresolved in this plan so far: 1" in again.output
    assert [m[0] for m in store.mutations] == ["1", "2"]
    retried = sync("--plan", str(p), "--resume", "--retry-failed")
    assert retried.exit_code == 0, retried.output
    assert [m[0] for m in store.mutations] == ["1", "2", "1"] and store.states["1"] == "SUBSCRIBED"


@respx.mock
def test_each_sync_run_gets_its_own_results_file(tmp_path, monkeypatch):
    sync_env(tmp_path, monkeypatch, FakeStore({"1": "NOT_SUBSCRIBED", "2": "NOT_SUBSCRIBED"}))
    p = plan_file(tmp_path, [("1", "SUBSCRIBED", ""), ("2", "SUBSCRIBED", "")])
    fixed = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("migtool.output.utc_now", lambda: fixed)  # both runs in the same second
    assert sync("--plan", str(p), "--limit", "1").exit_code == 0
    assert sync("--plan", str(p), "--resume").exit_code == 0
    files = list((tmp_path / "exports/shopify_us/consent-sync").glob("*.results.csv"))
    assert len(files) == 2 and set(results(tmp_path)) == {"1", "2"}


def test_consent_role_sends_only_the_email():
    role = dedupe.ROLES["consent"]
    row = {"Email Marketing Consent": "Subscribe", "Email Marketing Consent Timestamp": "2026-03-02T02:24:25Z",
           "email": "d14@x.com", "shopify_customer_id": "5", "first_name": "Changed"}
    p = dedupe.plan(role, [row], {}, "RUN", lambda e, ph: {"d14@x.com"})
    assert p.payloads == [{"email": "d14@x.com"}]


# --- D16: Shopify ignores changes dated before its current consent date ---------------------

@respx.mock
def test_sync_dates_at_sync_time_when_shopify_is_newer_and_detects_ignored_writes(tmp_path, monkeypatch):
    # 1: plan has an original date, but Shopify's live date is newer → sent at the sync time and applied.
    # 2: plan says "sync time" (blank date) → sent at the sync time.
    store = FakeStore({"1": "SUBSCRIBED", "2": "SUBSCRIBED"},
                      dates={"1": "2026-06-01T00:00:00Z", "2": "2026-01-01T00:00:00Z"})
    sync_env(tmp_path, monkeypatch, store)
    p = plan_file(tmp_path, [("1", "UNSUBSCRIBED", "2025-08-01T00:00:00Z"), ("2", "UNSUBSCRIBED", "")])
    result = sync("--plan", str(p))
    assert result.exit_code == 0, result.output
    r = results(tmp_path)
    assert {k: (v["outcome"], v["date_rule"]) for k, v in r.items()} == {"1": ("written", "sync time"),
                                                                       "2": ("written", "sync time")}
    assert store.states == {"1": "UNSUBSCRIBED", "2": "UNSUBSCRIBED"}
    assert all(m[1]["consentUpdatedAt"] > "2026-06-01" for m in store.mutations)


class IgnoringStore(FakeStore):
    """Accepts every write without an error but changes nothing."""

    def __call__(self, request):
        body = json.loads(request.content)
        if "customerEmailMarketingConsentUpdate" in body["query"]:
            cid = body["variables"]["input"]["customerId"].rsplit("/", 1)[-1]
            self.mutations.append((cid, body["variables"]["input"]["emailMarketingConsent"]))
            return httpx.Response(200, json={"data": {"customerEmailMarketingConsentUpdate": {
                "customer": {"id": f"gid://shopify/Customer/{cid}", "emailMarketingConsent": {
                    "marketingState": self.states[cid], "consentUpdatedAt": None}}, "userErrors": []}}})
        return super().__call__(request)


@respx.mock
def test_a_write_shopify_ignores_is_not_counted_as_written(tmp_path, monkeypatch):
    sync_env(tmp_path, monkeypatch, IgnoringStore({"1": "SUBSCRIBED"}))
    result = sync("--plan", str(plan_file(tmp_path, [("1", "UNSUBSCRIBED", "2025-01-01T00:00:00Z")])))
    assert result.exit_code == 1 and "ignored 1" in result.output
    assert results(tmp_path)["1"]["outcome"] == "ignored"
