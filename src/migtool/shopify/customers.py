"""Shopify customers: look up by email, with their marketing consent, and
compare with the matching Klaviyo profiles."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from migtool.klaviyo.client import KlaviyoClient
from migtool.shopify.client import ShopifyClient

SEARCH_BATCH = 25  # emails per search query
CUSTOMER_FIELDS = """
  id email phone firstName lastName state createdAt updatedAt tags numberOfOrders
  emailMarketingConsent { marketingState marketingOptInLevel consentUpdatedAt }
  smsMarketingConsent { marketingState marketingOptInLevel consentUpdatedAt consentCollectedFrom }
  defaultAddress { city provinceCode countryCodeV2 zip }
"""
QUERY = """query($q: String!, $after: String) {
  customers(first: 250, query: $q, after: $after) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}""" % CUSTOMER_FIELDS

COLUMNS = [
    "email", "shopify_customer_id", "shopify_state", "shopify_email_marketing", "shopify_email_opt_in_level",
    "shopify_email_consent_updated", "shopify_sms_marketing", "shopify_tags", "shopify_orders",
    "shopify_created", "shopify_updated", "shopify_country",
    "klaviyo_profile_id", "klaviyo_consent", "klaviyo_suppression", "klaviyo_can_receive_email",
    "klaviyo_accepts_marketing", "klaviyo_migration_hold", "match",
]

# Shopify states that mean "may be emailed" (the rest: NOT_SUBSCRIBED, UNSUBSCRIBED,
# REDACTED, INVALID, PENDING).
SHOPIFY_YES = {"SUBSCRIBED"}


def _search(emails: list[str]) -> str:
    return " OR ".join(f'email:"{e}"' for e in emails)


def customers_by_email(client: ShopifyClient, emails: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Customers keyed by lowercased email. Search matching isn't exact, so only
    customers whose email equals a requested one are kept."""
    wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
    found: dict[str, dict[str, Any]] = {}
    for i in range(0, len(wanted), SEARCH_BATCH):
        chunk = wanted[i:i + SEARCH_BATCH]
        for node in _pages(client, _search(chunk)):
            email = (node.get("email") or "").lower()
            if email in chunk:
                found[email] = node
    return found


def _pages(client: ShopifyClient, q: str) -> Iterator[dict[str, Any]]:
    after = None
    while True:
        data = client.query(QUERY, {"q": q, "after": after})["customers"]
        yield from data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            return
        after = data["pageInfo"]["endCursor"]


def klaviyo_profiles(client: KlaviyoClient, emails: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Klaviyo profiles keyed by lowercased email, with consent and the properties compared."""
    wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(wanted), 100):
        listed = ",".join(json.dumps(e) for e in wanted[i:i + 100])
        params = {"filter": f"any(email,[{listed}])", "additional-fields[profile]": "subscriptions",
                  "fields[profile]": "email,subscriptions,properties", "page[size]": "100"}
        for page in client.paginate("/profiles/", tier="L", params=params):
            for p in page["data"]:
                out[(p["attributes"].get("email") or "").lower()] = {"id": p["id"], **p["attributes"]}
    return out


def row(email: str, c: dict[str, Any] | None, k: dict[str, Any] | None) -> dict[str, Any]:
    em = (c or {}).get("emailMarketingConsent") or {}
    sms = (c or {}).get("smsMarketingConsent") or {}
    addr = (c or {}).get("defaultAddress") or {}
    mk = (((k or {}).get("subscriptions") or {}).get("email") or {}).get("marketing") or {}
    props = (k or {}).get("properties") or {}
    r = {
        "email": email,
        "shopify_customer_id": (c or {}).get("id", "").rsplit("/", 1)[-1],
        "shopify_state": (c or {}).get("state"),
        "shopify_email_marketing": em.get("marketingState"),
        "shopify_email_opt_in_level": em.get("marketingOptInLevel"),
        "shopify_email_consent_updated": em.get("consentUpdatedAt"),
        "shopify_sms_marketing": sms.get("marketingState"),
        "shopify_tags": ";".join((c or {}).get("tags") or []),
        "shopify_orders": (c or {}).get("numberOfOrders"),
        "shopify_created": (c or {}).get("createdAt"),
        "shopify_updated": (c or {}).get("updatedAt"),
        "shopify_country": addr.get("countryCodeV2"),
        "klaviyo_profile_id": (k or {}).get("id"),
        "klaviyo_consent": mk.get("consent"),
        "klaviyo_suppression": ";".join(s.get("reason", "") for s in mk.get("suppression") or []),
        "klaviyo_can_receive_email": mk.get("can_receive_email_marketing"),
        "klaviyo_accepts_marketing": props.get("Accepts Marketing"),
        "klaviyo_migration_hold": props.get("migration_hold"),
    }
    r["match"] = compare(r, has_customer=c is not None, has_profile=k is not None)
    return r


def compare(r: dict[str, Any], *, has_customer: bool, has_profile: bool) -> str:
    """How Shopify's email-marketing state lines up with Klaviyo's consent."""
    if not has_customer:
        return "no Shopify customer"
    if not has_profile:
        return "no Klaviyo profile"
    shop_yes = r["shopify_email_marketing"] in SHOPIFY_YES
    klav_yes = r["klaviyo_consent"] == "SUBSCRIBED"
    if shop_yes == klav_yes:
        return "same" if not (klav_yes and not r["klaviyo_can_receive_email"]) else "same (Klaviyo suppressed)"
    return "Shopify yes / Klaviyo no" if shop_yes else "Shopify no / Klaviyo yes"
