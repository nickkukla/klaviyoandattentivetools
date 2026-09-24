# API notes

Findings from the phase 1 checks (`docs/BUILD_PLAN.md`). Checked 2026-09-24 unless noted. Klaviyo calls use revision `2026-07-15`.

Status: ✅ answered · ⚠️ answered, affects the spec · ⏳ pending

## Accounts

| Instance | Result |
|---|---|
| `klaviyo_ca` | ✅ `Ka6Lvr` (Left On Friday Canada). First key returned 401; replaced. |
| `klaviyo_us` | ✅ `KF4XLe` (Left On Friday) |
| `klaviyo_sandbox` | ✅ `T2aEdf` (Left In Friday Dev), as expected |
| `attentive_ca` | ✅ `leftonfriday-ca.attn.tv`, app `0xb8access` |
| `attentive_us` | ✅ `leftonfriday.attn.tv`, app `0xb8access` |
| `stoq_dev`, `stoq_us` | No auth to check; covered by the STOQ check |

- Klaviyo `whoami` reads `GET /api/accounts/` → `data[0].id` and `attributes.contact_information.organization_name`. Rate tier XS (1/s, 15/min).
- Attentive `/v1/me` and `/v2/me` both work and return `companyId`, `companyName`, `applicationName`, `attentiveDomainName`, `contactEmail`.

## Attentive

- ⚠️ **List Segments (`GET /v2/segments`) returns 0 segments in both accounts.** Response shape is `{segments, cursor, hasMore}`. Both accounts do have UI segments (confirmed by the user), and filters (`name`, `limit=1000`, `updatedSince=2015-…`) change nothing, so this endpoint only lists API-created segments. The docs don't say so. As a result so `attentive segments export` can't list existing CA segments. The duplicate-name guard still works because it looks for segments the tool itself created.
- **Decision (2026-09-24):** Attentive dropped from the tool entirely; segments are handled by hand. The `whoami` results above were the last Attentive calls.

## Klaviyo segments

- ✅ Rule definitions: `GET /api/segments/?fields[segment]=name,definition` returns `definition.condition_groups[].conditions[]` at revision `2026-07-15`. All 66 `klaviyo_us` and 53 `klaviyo_ca` segments have one.
- Condition types seen (`profile-region` only in `klaviyo_us`):

  | `type` | Keys | Label |
  |---|---|---|
  | `profile-metric` | `metric_id`, `measurement`, `measurement_filter`, `timeframe_filter`, `metric_filters` | from the metric (below) |
  | `profile-property` | `property`, `filter` | none |
  | `profile-marketing-consent` | `consent` | none |
  | `profile-group-membership` | `group_ids`, `is_member`, `timeframe_filter` | none (list/segment membership) |
  | `profile-postal-code-distance` | `postal_code`, `country_code`, `unit`, `filter` | none |
  | `profile-region` | `region`, `in_region` | none |

- ✅ Member count: `additional-fields[segment]=profile_count`.
- ✅ Metric → integration: `GET /api/metrics/?fields[metric]=name,integration` gives `integration.{id,key,name,category}`. `category` is sometimes a string (`"Internal"`, `"eCommerce"`) and sometimes an object (`{"category": "Shipping", "id": 9}`). Metrics can't be filtered by name (only `integration.name`, `integration.category`), so match names client-side.
- ✅ **Decision (2026-09-24):** site activity is decided by event name, whatever the integration (so API-sourced Active on Site / Viewed Product / Added to Cart count), and includes Checkout Started; Klaviyo subscription events get no label. Spec updated.
- Metrics used by segments in either account:

  | Metric | Integration | Spec label |
  |---|---|---|
  | Opened / Clicked / Received Email | Klaviyo | Engagement |
  | Placed Order, Ordered Product | Shopify | Engagement |
  | Added to Cart | Shopify | Site activity |
  | Checkout Started (`klaviyo_ca` only) | Shopify | Site activity |
  | Active on Site, Viewed Product, Added to Cart | **API** | Site activity |
  | Subscribed to List, Subscribed to / Unsubscribed from Email Marketing, Subscribed to Back in Stock | Klaviyo | None |
  | Bought Ticket (Eventbrite), Loop Return Created (Loop Returns), Customer signed up for alert (STOQ) | other | Third-party |

## Klaviyo Back in Stock

- ✅ Metric `Subscribed to Back in Stock` (integration Klaviyo) in both accounts: `klaviyo_ca` `MKwtiC`, `klaviyo_us` `KWTuwh`.
- ✅ Event properties (same keys in both accounts): `SKU`, `VariantId`, `ProductID`, `ProductName`, `VariantName`, `Price`, `Categories`, `Channels` (e.g. `["EMAIL"]`), `platform` (`Shopify`), `$internal`, `$event_id`. **The SKU is on the event**, so no catalog lookup is needed to get it.
- ✅ Read with `GET /api/events/?filter=and(equals(metric_id,"…"),greater-than(datetime,…))&include=profile`; the datetime filter works for `--since`.
- ⚠️ **Catalog lookup isn't possible.** The Catalogs API only serves custom (`$custom`) catalogs; Shopify-synced catalogs aren't exposed (`/catalog-items` and `/catalog-variants` are empty in `klaviyo_us` and `klaviyo_sandbox`). **Decision (2026-09-24):** drop the in-stock check; STOQ handles in-stock variants. Spec updated.
- STOQ columns from Klaviyo (`klaviyo_ca` profiles carry `email`, `first_name`, `locale` and properties `Accepts Marketing`, `Shopify Tags`, `SubscribeSource`, `$consent`, `$source`): `Name` ← `first_name`; `Language` ← `locale`; `Accepts marketing` ← profile property `Accepts Marketing` (Shopify's flag); `Quantity` ← no field on the event (left blank).

## Klaviyo profiles and suppressions

- ✅ `additional-fields[profile]=subscriptions` gives `subscriptions.email.marketing.{consent, consent_timestamp, method, method_detail, custom_method_detail, double_optin, last_updated, can_receive_email_marketing, suppression[], list_suppressions[]}`. Each `suppression` item is `{reason, timestamp}` (e.g. `UNSUBSCRIBE`, `USER_SUPPRESSED`).
- ✅ Filters that work on `/api/profiles/`: `greater-than(updated,…)`, `equals(subscriptions.email.marketing.suppression.reason,"…")`, `greater-than(subscriptions.email.marketing.suppression.timestamp,…)`.
- ✅ List members: `/api/lists/{id}/profiles/` accepts `greater-than(joined_group_at,…)`.
- ⚠️ **What moves `updated`** (sandbox, 2026-09-24):
  - Subscribing an existing profile (historical import): **yes** (`test04` 19:01:36 → 19:07:25).
  - Unsubscribing (bulk subscription delete): **no**. The unsubscribe shows up only as a `suppression` item `{reason: UNSUBSCRIBE, timestamp}`, so `--since` on profile `updated` misses it; the suppressions export's `suppression.timestamp` filter catches it.
  - Joining a list: **no**; use `joined_group_at` (as the spec already says).
  - Suppression: ⏳ bulk suppression jobs stuck (see below).
  - Consequence: the catch-up run must combine the profiles export (`updated`) with the suppressions export (`suppression.timestamp`); the profiles export alone misses unsubscribes.

## Klaviyo writes

| Endpoint | Batch limit | Rate | Notes |
|---|---|---|---|
| Bulk profile import (`profile-bulk-import-job`) | 10,000 profiles, 5 MB | 10/s, 150/min | Returns a job (`queued` → `complete`, with `total_count`, `completed_count`, `failed_count`; `/import-errors/` lists failures). Creates missing profiles. Optional `relationships.lists`. |
| Bulk subscribe (`profile-subscription-bulk-create-job`) | 1,000 profiles | 75/s, 750/min | **202 with no body, so no job to track**; check the profiles afterwards. `historical_import: true` needs `consented_at` in the past. |
| Bulk unsubscribe (`profile-subscription-bulk-delete-job`) | – | 75/s, 750/min | 202 with no body. |
| Bulk suppress (`profile-suppression-bulk-create-job`) | 100 emails | 75/s, 750/min | Returns a job. Docs: creates missing profiles, suppression applies whatever the consent. |
| Create list (`POST /lists/`) | – | S | Accepts `opt_in_process: double_opt_in`. |

Sandbox results (`klaviyo_sandbox`, test lists `migtool phase1 DOI` `WgThjd` (double opt-in) and `migtool phase1 list-add` `QNYnsn`, addresses `test01`–`test06@0xb8.net`):

- ✅ **Historical-import subscribe skips double opt-in.** `test01`–`test03` into the double-opt-in list: `SUBSCRIBED` and on the list immediately, `consent_timestamp` = the `consented_at` sent (2024-01-15), `method` `API`, `$source` = `custom_source`. The legacy `$consent_timestamp` property shows the import time, so exports must read `subscriptions.email.marketing.consent_timestamp`.
- ✅ **List relationship on bulk import leaves consent alone.** `test01` (subscribed) kept `SUBSCRIBED`/2024-01-15, `test04` stayed `NEVER_SUBSCRIBED`, and `test05` (no profile) was created `NEVER_SUBSCRIBED`. All three joined the list. A second identical run changed nothing (same `joined_group_at`, same consent).
- ⏳ **Bulk suppression.** Two jobs (`01M3ACS7…` for `test02`, submitted 19:02; `01M3AD1C…` for `test02` + `test06`, submitted 19:07) showed `total_count` 0 for about 20 minutes. At 19:28 they read `processing`, `total_count` 1 and 2, `completed_count` 0, `skipped_count` 1 and 2. `test06` was created at 19:26 as `NEVER_SUBSCRIBED` but isn't suppressed; `test02` is still `SUBSCRIBED` and unsuppressed. The job has no errors sub-resource (404), so no reason is given. Unknown whether this is final, or something specific to the test account (`test_account: true`). A third job using the list form (`relationships.list` → `migtool phase1 list-add`, job `01M3AED2…`, 19:31) was submitted to compare.
  - **Deferred (2026-09-24):** not settled in phase 1. Only tried in `klaviyo_sandbox`; untested elsewhere. Revisit with the list-form result, a one-address test in `klaviyo_us` (needs approval), or Klaviyo support. Fallback: suppressions handled by hand in the Klaviyo UI.
  - Either way, suppression jobs can take 20+ minutes to start, so `suppressions import` must poll patiently and report skipped counts.
- ✅ **No emails sent.** The test profiles have only `Subscribed to List`, `Subscribed to Email Marketing` and (for `test03`) `Unsubscribed from Email Marketing` events, with no `Received Email`. Historical-import subscribe events are **backdated** to `consented_at`. The bulk import list add logged no events.

## STOQ

Source: https://docs.stoqapp.com/v1/ (checked 2026-09-24).

**Decision (2026-09-24):** `stoq import` dropped, along with the `stoq_dev`/`stoq_us` instances and `.env` variables. The BIS export file is uploaded by hand in STOQ admin. Findings kept for reference:

- ⚠️ **`.env` values are admin URLs, not shop domains.** Both `STOQ_*_SHOP_DOMAIN` are `https://admin.shopify.com/store/<handle>`. The header needs `<handle>.myshopify.com`; both derived domains exist.
- ⚠️ **The create-intent API takes Shopify IDs, not a SKU.** `POST https://app.stoqapp.com/api/v1/intents.json`, header `X-Shopify-Shop-Domain`, no API key. Body: `intent.{shopify_variant_id (required), shopify_product_id (required), shopify_market_id, channel (email|sms|push, required), quantity (required), source: "api" (required)}`, `customer.{email, name, phone, country, country_code, accepts_marketing, locale, shopify_market_id, shopify_customer_id}`, optional `product.{title, variant_title, vendor, sku, variant_count}` (display only). Market is a numeric Shopify Market ID; there's no inventory-location field. The docs say an intent is unique per customer, variant and market.
- Sending only a SKU returns 404 `{"error":"Not found"}`, the same as a fake variant ID or an unknown shop.
- The v1 docs don't mention rate limits or duplicate responses (the 360 points/min figure isn't on this page).
- SKU → US IDs: the US storefront's public `/products.json` gives `variants[].{id, product_id, sku}` (235 products and 1,088 variants on page 1), but only for published products. The dev storefront is password-protected (401).
