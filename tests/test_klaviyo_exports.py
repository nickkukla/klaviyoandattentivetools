import csv
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from typer.testing import CliRunner

from migtool.cli import app
from migtool.klaviyo import profiles, segments
from migtool.klaviyo.client import KlaviyoClient
from migtool.config import Secret
from migtool.output import ResumableExport
from migtool.state import StateStore

API = "https://a.klaviyo.com/api"


def profile(pid, email, *, updated="2026-09-01T00:00:00+00:00", props=None, suppression=None):
    return {
        "type": "profile", "id": pid,
        "attributes": {
            "email": email, "phone_number": None, "first_name": "A", "updated": updated,
            "created": "2025-01-01T00:00:00+00:00",
            "location": {"city": "Toronto", "country": "CA"},
            "properties": props or {},
            "subscriptions": {"email": {"marketing": {
                "consent": "SUBSCRIBED", "consent_timestamp": "2024-01-15T12:00:00+00:00",
                "method": "API", "suppression": suppression or [], "list_suppressions": [],
            }}},
        },
    }


def page(data, nxt=None):
    return {"data": data, "links": {"next": nxt}}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_flatten_profile():
    row = profiles.flatten(profile(
        "p1", "a@example.com", props={"Language": "fr", "tags": ["x"]},
        suppression=[{"reason": "UNSUBSCRIBE", "timestamp": "2025-05-16T13:08:49.75+00:00"},
                     {"reason": "HARD_BOUNCE", "timestamp": "2024-01-01T00:00:00+00:00"}],
    ))
    assert row["email"] == "a@example.com"
    assert row["location.city"] == "Toronto"
    assert row["properties.Language"] == "fr"
    assert row["properties.tags"] == ["x"]
    assert row["consent"] == "SUBSCRIBED"
    assert row["consent_timestamp"] == "2024-01-15T12:00:00Z"
    assert row["updated"] == "2026-09-01T00:00:00Z"
    assert (row["suppression_reason"], row["suppression_timestamp"]) == ("UNSUBSCRIBE", "2025-05-16T13:08:49Z")


@pytest.mark.parametrize("name,integration,expected", [
    ("Opened Email", "klaviyo", "engagement"),
    ("Placed Order", "shopify", "engagement"),
    ("Active on Site", "api", "site_activity"),
    ("Viewed Product", "shopify", "site_activity"),
    ("Checkout Started", "shopify", "site_activity"),
    ("Subscribed to List", "klaviyo", None),
    ("Subscribed to Back in Stock", "klaviyo", None),
    ("Bought Ticket", "eventbrite", "third_party"),
    ("Custom Thing", "api", "third_party"),
])
def test_label(name, integration, expected):
    assert segments.label(name, integration) == expected


def test_labels_from_definition():
    definition = {"condition_groups": [
        {"conditions": [{"type": "profile-metric", "metric_id": "m1"}]},
        {"conditions": [{"type": "profile-property", "property": "email"},
                        {"type": "profile-metric", "metric_id": "m2"},
                        {"type": "profile-metric", "metric_id": "gone"}]},
    ]}
    known = {"m1": ("Opened Email", "klaviyo"), "m2": ("Bought Ticket", "eventbrite")}
    assert segments.labels(definition, known) == {
        "engagement": True, "site_activity": False, "third_party": True,
        "events": "Bought Ticket; Opened Email; unknown metric gone",
    }
    assert segments.labels(None, known)["events"] == ""


def make_export(tmp_path, **kwargs):
    return ResumableExport(
        "klaviyo_sandbox", "profiles", profiles.COLUMNS, staged=True,
        store=StateStore(tmp_path / "state"), base=tmp_path / "exports",
        echo=lambda _: None, **kwargs,
    )


@respx.mock
def test_profile_export_resumes_without_duplicates(tmp_path):
    p1 = page([profile("p1", "a@example.com", props={"a": 1})], f"{API}/profiles/?page[cursor]=2")
    p2 = page([profile("p2", "b@example.com", props={"b": 2})])
    route = respx.get(f"{API}/profiles/").mock(side_effect=[
        httpx.Response(200, json=p1), httpx.Response(500), httpx.Response(500),
        httpx.Response(500), httpx.Response(500), httpx.Response(500), httpx.Response(500),
    ])
    client = KlaviyoClient(Secret("pk_x"))
    client._http._sleep = lambda s: None
    exp = make_export(tmp_path)
    with pytest.raises(Exception):
        profiles.export(client, exp)
    assert exp.rows == 1

    route.side_effect = [httpx.Response(200, json=p2)]
    exp = make_export(tmp_path, resume=True)
    assert "cursor" in exp.cursor
    profiles.export(client, exp)
    exp.finish()
    rows = read_csv(tmp_path / "exports/klaviyo_sandbox/profiles" / f"{exp.run.stamp}.csv")
    assert [r["id"] for r in rows] == ["p1", "p2"]
    assert rows[0]["properties.a"] == "1" and rows[1]["properties.b"] == "2"
    assert list(rows[0])[:2] == ["id", "email"]
    assert not (tmp_path / "exports/klaviyo_sandbox/profiles" / f"{exp.run.stamp}.jsonl").exists()


@respx.mock
def test_profile_export_since_filters_by_updated(tmp_path):
    route = respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json=page([])))
    exp = make_export(tmp_path)
    profiles.export(KlaviyoClient(Secret("pk_x")), exp, since=datetime(2026, 9, 1, tzinfo=UTC))
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["filter"] == ["greater-than(updated,2026-09-01T00:00:00Z)"]


@respx.mock
def test_segment_export_since_filters_client_side(tmp_path):
    respx.get(f"{API}/segments/S1/profiles/").mock(return_value=httpx.Response(200, json=page([
        profile("old", "o@example.com", updated="2026-08-01T00:00:00+00:00"),
        profile("new", "n@example.com", updated="2026-09-10T00:00:00+00:00"),
    ])))
    exp = make_export(tmp_path)
    skipped = profiles.export(
        KlaviyoClient(Secret("pk_x")), exp, segment_id="S1", since=datetime(2026, 9, 1, tzinfo=UTC)
    )
    assert (exp.rows, skipped) == (1, 1)


@respx.mock
def test_ambiguous_segment_name_lists_ids():
    respx.get(f"{API}/segments/").mock(return_value=httpx.Response(200, json=page([
        {"id": "A", "attributes": {"name": "VIP"}}, {"id": "B", "attributes": {"name": "vip"}},
    ])))
    with pytest.raises(LookupError, match="A \\(VIP\\), B \\(vip\\)"):
        profiles.resolve_segment(KlaviyoClient(Secret("pk_x")), "VIP")


@respx.mock
def test_suppressions_export_one_row_per_suppression_since(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    route = respx.get(f"{API}/profiles/").mock(return_value=httpx.Response(200, json=page([
        profile("p1", "a@example.com", suppression=[
            {"reason": "UNSUBSCRIBE", "timestamp": "2026-09-05T00:00:00+00:00"},
            {"reason": "HARD_BOUNCE", "timestamp": "2026-08-01T00:00:00+00:00"},
        ]),
    ])))
    result = CliRunner().invoke(app, ["klaviyo", "suppressions", "export", "--instance",
                                      "klaviyo_sandbox", "--since", "2026-09-01T00:00:00Z"])
    assert result.exit_code == 0, result.output
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["filter"] == ["greater-than(subscriptions.email.marketing.suppression.timestamp,2026-09-01T00:00:00Z)"]
    [path] = (tmp_path / "exports/klaviyo_sandbox/suppressions").glob("*.csv")
    assert read_csv(path) == [{"email": "a@example.com", "profile_id": "p1",
                               "reason": "UNSUBSCRIBE", "timestamp": "2026-09-05T00:00:00Z"}]


@respx.mock
def test_lists_export_writes_both_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    respx.get(f"{API}/lists/").mock(return_value=httpx.Response(200, json=page([
        {"id": "L1", "attributes": {"name": "News", "created": "2025-01-01T00:00:00+00:00",
                                    "updated": "2025-01-02T00:00:00+00:00", "opt_in_process": "single_opt_in"}},
    ])))
    members = respx.get(f"{API}/lists/L1/profiles/").mock(return_value=httpx.Response(200, json=page([
        {"id": "p1", "attributes": {"email": "a@example.com", "joined_group_at": "2026-09-02T00:00:00+00:00"}},
    ])))
    result = CliRunner().invoke(app, ["klaviyo", "lists", "export", "--instance", "klaviyo_sandbox",
                                      "--since", "2026-09-01T00:00:00Z"])
    assert result.exit_code == 0, result.output
    q = parse_qs(urlparse(str(members.calls.last.request.url)).query)
    assert q["filter"] == ["greater-than(joined_group_at,2026-09-01T00:00:00Z)"]
    d = tmp_path / "exports/klaviyo_sandbox/lists"
    [lists_csv] = d.glob("*.lists.csv")
    [members_csv] = d.glob("*.list_members.csv")
    assert read_csv(lists_csv)[0]["member_count"] == "1"
    assert read_csv(members_csv) == [{"list_id": "L1", "list_name": "News", "profile_id": "p1",
                                      "email": "a@example.com", "joined_group_at": "2026-09-02T00:00:00Z"}]


def test_bad_since_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLAVIYO_SANDBOX_API_KEY", "pk_x")
    result = CliRunner().invoke(app, ["klaviyo", "suppressions", "export", "--instance",
                                      "klaviyo_sandbox", "--since", "yesterday"])
    assert result.exit_code != 0
    assert "ISO 8601" in result.output
