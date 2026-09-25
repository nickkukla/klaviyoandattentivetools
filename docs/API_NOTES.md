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

- ✅ Member count: `additional-fields[segment]=profile_count` works only on a single `GET /segments/{id}/`, not on the list endpoints (`/segments/`, `/lists/` reject it). The exports count members while writing `*_members.csv` instead.
- `/segments/{id}/profiles/` and `/lists/{id}/profiles/` accept `additional-fields[profile]=subscriptions` and filter on `joined_group_at`, but **not** on `updated` (filterable: `_kx`, `email`, `id`, `joined_group_at`, `phone_number`, `push_token`), so `profiles export --segment --since` filters on `updated` client-side.
- Suppression job list sorts by `created`/`-created` (not `created_at`).
- ⚠️ **Paging order and duplicates** (decoded `page[cursor]`, 2026-09-24):
  - `/profiles/` with no filter pages by `id` (immutable, new profiles last): stable.
  - With `greater-than(updated,…)` it pages by `updated`; with the suppression-timestamp filter, by suppression timestamp. A record that changes mid-export moves later and is **read twice, never skipped**. The first `klaviyo_ca` suppressions export had 102 such duplicates in 106,262 rows.
  - `sort` can't be combined with the suppression filter ("You may not filter on subscriptions…").
  - `/segments/{id}/profiles/` pages by an internal id: stable.
  - So profile and suppression exports keep only the last copy of each record (`unique_by`) when finalizing, and record `duplicates_dropped` in the manifest.
- Page latency, not the rate limit, sets export speed: ~0.3 s per 100 profiles plain, ~0.6 s with `subscriptions`, ~1.6 s with the suppression filter. Full `klaviyo_ca` profile export ≈ 8,000 profiles/min.
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
- ✅ **Bulk suppression works, but took two to four hours to apply, and its job status is wrong** (corrected 2026-09-24; earlier notes said it "skipped every profile"):

  | Job | Submitted | Emails | Suppressed at | Job status afterwards |
  |---|---|---|---|---|
  | `01M3ACS7…` | 19:02 | `test02` | 22:58 | `processing`, total 1, skipped 1 |
  | `01M3AD1C…` | 19:07 | `test02`, `test06` | `test06` 23:00 | `processing`, total 2, skipped 2 |
  | `01M3AED2…` (list form) | 19:31 | `test01`, `test04`, `test05` | 23:19 | `processing`, total 3, skipped 3 |

  - Each became a `USER_SUPPRESSED` suppression. Consent stayed `SUBSCRIBED` where it was, but `can_receive_email_marketing` went false: suppression wins over consent, as the spec needs.
  - The job never leaves `processing`, sets `completed_at` early, and reports every profile as `skipped` even when they are later suppressed. **Don't use the job status to judge the result.** Check the profiles instead (`klaviyo suppressions check`).
  - The missing profile in job 2 (`test06`) was created within about 20 minutes and suppressed about 4 hours later.
  - Latency in `klaviyo_us` is unknown (no production writes during development). The one-address pilot in the real run confirms it.
- ✅ **No emails sent.** The test profiles have only `Subscribed to List`, `Subscribed to Email Marketing` and (for `test03`) `Unsubscribed from Email Marketing` events, with no `Received Email`. Historical-import subscribe events are **backdated** to `consented_at`. The bulk import list add logged no events.

## STOQ

Source: https://docs.stoqapp.com/v1/ (checked 2026-09-24).

**Decision (2026-09-24):** `stoq import` dropped, along with the `stoq_dev`/`stoq_us` instances and `.env` variables. The BIS export file is uploaded by hand in STOQ admin. Findings kept for reference:

- ⚠️ **`.env` values are admin URLs, not shop domains.** Both `STOQ_*_SHOP_DOMAIN` are `https://admin.shopify.com/store/<handle>`. The header needs `<handle>.myshopify.com`; both derived domains exist.
- ⚠️ **The create-intent API takes Shopify IDs, not a SKU.** `POST https://app.stoqapp.com/api/v1/intents.json`, header `X-Shopify-Shop-Domain`, no API key. Body: `intent.{shopify_variant_id (required), shopify_product_id (required), shopify_market_id, channel (email|sms|push, required), quantity (required), source: "api" (required)}`, `customer.{email, name, phone, country, country_code, accepts_marketing, locale, shopify_market_id, shopify_customer_id}`, optional `product.{title, variant_title, vendor, sku, variant_count}` (display only). Market is a numeric Shopify Market ID; there's no inventory-location field. The docs say an intent is unique per customer, variant and market.
- Sending only a SKU returns 404 `{"error":"Not found"}`, the same as a fake variant ID or an unknown shop.
- The v1 docs don't mention rate limits or duplicate responses (the 360 points/min figure isn't on this page).
- SKU → US IDs: the US storefront's public `/products.json` gives `variants[].{id, product_id, sku}` (235 products and 1,088 variants on page 1), but only for published products. The dev storefront is password-protected (401).

## Phase 3 sandbox trial (2026-09-24)

- **Consent timestamps:** historical-import subscribe sets the original `consented_at` on profiles not currently subscribed (`test06`, `test10`, `test13`). On a profile **already subscribed**, it keeps the existing timestamp (`test02`: file 2020, kept 2024; `test04`: file 2019, kept 2025).
- **Unsubscribe** (`profile-subscription-bulk-delete-jobs`) applies at once: consent `UNSUBSCRIBED`, a suppression item `{reason: UNSUBSCRIBE}` stamped with the current time, and the profile shows in Klaviyo's suppressed list. It is the immediate fallback while suppression jobs apply, or if they don't (`--as-unsubscribe`).
- **Bulk import** with only `email` + list relationship leaves every other field and consent untouched; the CA profile `id` and `$`-properties are never sent; `external_id` in the destination stays as it was (`test04` kept `US-04`, `ca_external_id=CA-04`).
- **Property types** round-trip through the CSV: `3` → number, `01234` → text, `["vip","swim"]` → list.
- **No emails:** no `Received Email` events on any trial profile, including those subscribed into a double-opt-in list.
- **Catch-up:** after changing `test13` (property), `test15` (subscribe) and `test10` (unsubscribe), `profiles export --since` returned exactly `test13` and `test15`, and `suppressions export --since` exactly `test10`, out of ~153k sandbox profiles.

## Phase 4 Back in Stock export (2026-09-24)

- `klaviyo_ca`: 43,445 `Subscribed to Back in Stock` events (2021-01-11 → 2026-09-24), all with a `SKU` and a linked profile. 37,783 have `Channels: ["EMAIL"]`, and 5,662 have no `Channels` key. 8,935 carry a `Tags` property. No profile has a `Language` property; locales are mostly `en-CA`, `en` or blank.
- ⚠️ `GET /events/?include=profile&additional-fields[profile]=subscriptions` is accepted, but every included profile comes back with `subscriptions: null`. Consent is read separately with `GET /profiles/?filter=any(id,[…])&additional-fields[profile]=subscriptions` (≤100 ids per call, cached).
- Export: 40,015 rows (5,214 distinct SKUs), 3,430 excluded (3,423 older duplicates, 7 profiles with no email). `Accepts marketing`: 24,057 true, 15,958 false. 25 random rows matched their Klaviyo events (SKU, email, date, consent).
- STOQ's admin import ("Import your waitlist"): CSV only; headers matching the template map automatically; `SKU` can be a SKU or a Shopify variant ID; `GDPR confirmed`/`Accepts marketing` take `true`/`false` (blank = false); `Language` takes a locale code; `Date` takes `dd/mm/yyyy` or `mm/dd/yyyy`; `Quantity` defaults to 1; it sends no email; it skips a customer already waiting on the same variant; and it blocks test or disposable addresses (`example.com`, `mailinator.com` and so on).

### STOQ upload side effect in Klaviyo (observed 2026-09-25)

After the 10-row trial upload to the dev store's STOQ admin (01:09–01:10 UTC), the dev store's STOQ integration pushed the signups into `klaviyo_sandbox`. It set `StoqAcceptsMarketing`, `StoqBackInStock` and `StoqLocaleCountry` on all 10 profiles and logged "Customer signed up for alert (STOQ)". It also **subscribed six of them to email marketing (`method: API`)**: some had been unsubscribed, two had `Accepts marketing = false` in the file, and one (`test05`) was "Manually Unsuppressed" first. Profiles that were already subscribed and suppressed stayed suppressed. No `Received Email` events were seen.

Decision: handled by hand. Before uploading, the user compares the BIS emails against the Klaviyo profile exports and removes signups from people who are unsubscribed or never subscribed.

## Review-fix trial (2026-09-25, `klaviyo_sandbox`)

A 4-row file saved like Excel "CSV UTF-8" (byte-order mark, CRLF), run `20260925T022655Z-83dd`:
- The BOM file read correctly.
- A bad `#number` cell (`three`) was reported and that row wasn't sent.
- Typed properties arrived exactly: `code` stayed the text `"123"`, `orders` became the number 5, `vip` true, `tags` a list.
- ⚠️ **Klaviyo refuses a backdated subscribe older than a newer unsubscribe:** `400 Invalid input.: backdated consent date [2020-01-11 …] is before current unsubscription date [2026-09-24 …]`. The subscribe batch was split, the other row went through, and the refused row is in the errors file. In the migration this means a CA subscriber who has since unsubscribed in US stays unsubscribed.
- ⚠️ **Klaviyo silently drops an invalid phone number on bulk import** (`+1234`: the profile was imported and the phone not stored, with no import error). The tool can't detect this; the external dedupe should validate phones.
- `test12`'s historical subscribe was accepted (202) but not visible about 40 s later, unlike the Phase 3 trial (about 15 s). `test12` also has a suppression job pending from the Phase 3 trial. Rechecked at 02:50 UTC.

## Klaviyo error pointers (2026-09-25, refused requests to `klaviyo_sandbox`)

Klaviyo refuses a bulk request as a whole and says where the problem is in `errors[].source.pointer`:

| Problem | Status | `source.pointer` |
|---|---|---|
| Backdated subscribe before a newer unsubscribe (row) | 400 | `/data/attributes/profiles/data/0/attributes/subscriptions/email/marketing/consented_at` |
| Malformed email on bulk import (row) | 400 | `/data/attributes/profiles/data/0/attributes/email` |
| Unknown list ID on bulk import (request) | 400 | `/data` ("List ID … does not exist.") |
| Unknown list ID on bulk subscribe (request) | 400 | `/data/relationships/list/data/id` ("List not found with id …") |

The index in a row pointer is the profile's position in the request. The writer drops exactly those rows and resends the rest; any other pointer stops the run. Re-running the bad-rows trial confirmed both: `test11` alone refused (others applied); a made-up `--list-id` aborted after one request with the manifest recorded. Nothing was changed by the four probes.

## Suppression check results (2026-09-25)

`suppressions check` on the Phase 3 step 3 file at 02:50 UTC (4h06m after submission): `test01` and `test02` were suppressed. `test03` and `test05` weren't, because the STOQ trial upload had re-subscribed them. `test12` wasn't suppressed, but the review-fix trials had re-subscribed it at 02:26–02:40 (its historical subscribe finally showed at 2020-01-12), so the result is inconclusive. The job `…K5FE3E` still reads `processing`, total 5, skipped 5. Klaviyo logs no profile event when a bulk suppression applies.

Two later suppression jobs for `test14` (submitted 02:27 and 02:40 UTC by the review trials) are a clean test: nothing else touches `test14`.

### Isolated suppression result: `test14` (2026-09-25)

`test14@0xb8.net` (touched by nothing else) was sent to the suppression endpoint by `profiles import` for an older hard bounce. Its first suppression job was submitted at 02:27:09 UTC, and a `USER_SUPPRESSED` suppression appeared at **04:32:17 UTC (about 2 h)**, next to its earlier `UNSUBSCRIBE`. `can_receive_email_marketing` is false. The jobs still read `processing`. The time to apply in the sandbox has ranged from about 2 to 4 hours, so allow a few hours when checking the migration pilot.
