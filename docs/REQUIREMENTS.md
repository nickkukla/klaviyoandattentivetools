# Klaviyo, Attentive and STOQ migration tools — requirements

## Context

I'm running a migration, and part of that is merging objects and records from the Canada Klaviyo and Attentive accounts into the corresponding US accounts, plus moving Back in Stock signups into STOQ.

Instance names used throughout:

| Instance | Role |
|---|---|
| `klaviyo_ca`, `attentive_ca` | Source (Canada) |
| `klaviyo_us`, `attentive_us` | Destination (US, the merged store) |
| `klaviyo_sandbox` | Optional Klaviyo test account, if one is granted (requested, not guaranteed) |
| `stoq_dev` | STOQ on our Shopify dev store, for testing |
| `stoq_us` | STOQ on the merged/destination US Shopify store |

The tool is a command-line app that I run directly or ask Claude Code to run from a session. Every command targets a named instance.

API references:
- Attentive: https://docs.attentive.com/reference/test-authentication-v2
- Klaviyo: https://developers.klaviyo.com/en/reference/api_overview
- STOQ: https://docs.stoqapp.com/ and https://help.stoqapp.com/back-in-stock/migrate-klaviyo-back-in-stock-signups/

## General

### Runtime and configuration
- Python 3.12, `uv`, `typer` CLI. Runs locally by hand (WSL). No scheduler, no server.
- Secrets live in a git-ignored `.env`, one per instance: `KLAVIYO_CA_API_KEY`, `KLAVIYO_US_API_KEY`, `KLAVIYO_SANDBOX_API_KEY` (optional), `ATTENTIVE_CA_API_KEY`, `ATTENTIVE_US_API_KEY`, `STOQ_DEV_SHOP_DOMAIN`, `STOQ_US_SHOP_DOMAIN`. Keys are never logged.
- Output goes to `./exports/<instance>/<object>/<timestamp>.{csv,jsonl}` with a `manifest.json` of record counts per run.
- A README documents every command and flag, with an example of each, and the STOQ CSV preparation steps.

### Write safety
- Every write command names its target with `--to <instance>`. Before writing it prints the account name, the target instance and the record count, and asks the user to type the instance name to confirm.
- `--yes` skips the prompt for scripted runs, including runs started by Claude.
- Writes into a `_ca` instance also require `--allow-write-to-source`.
- Dry-run mode exists only for the Attentive segment membership upload. Everything else is either an export (for archiving, or to check or edit data) or an import of a previously exported file.

### Errors and retries
- Rate-limit (429) and server (5xx) errors are retried with increasing waits, honouring the service's `Retry-After`, up to 6 attempts.
- Per-record errors go to `<run>.errors.csv` and the run continues.
- Asynchronous bulk jobs (Klaviyo imports, Attentive segment members) are tracked, and their per-record errors go to the same errors file.
- Every run ends with a summary of counts (read, written, skipped, failed) and exits non-zero if anything failed.
- Long exports and imports save their position and support `--resume`.

## Attentive

Attentive's public API (REST and GraphQL) is mostly write-only. The only read endpoints relevant to this project are segment metadata.

### Segments
Uses the Segments (Open Beta) and Bulk Segment Operations (Open Beta) APIs:
[List Segments](https://docs.attentive.com/reference/listsegments), [Create Segment](https://docs.attentive.com/reference/createsegment), [Add Bulk Segment Members](https://docs.attentive.com/reference/postbulksegmentmembers), [Bulk job status](https://docs.attentive.com/reference/getbulkjobstatus). If the beta APIs fail or prove error-prone, we fall back to a manual process in the Attentive UI.

- **No automated CA → US migration.** List Segments returns only `externalId`, `name`, `description`, `created` and `updated`: no rules, counts or members, and there is no endpoint to read members. Segment membership will be requested from our Attentive customer success manager and supplied as CSV files.
- **Export:** `attentive segments export --instance <attentive_ca|attentive_us>` saves all segments (metadata only) to CSV.
- **Upload:** `attentive segments upload --to <instance> --file <csv> [--segment <name>] [--dry-run]` uploads one CSV per segment, with columns `segment_name, email, phone`.
  - It creates an empty, static segment in the target, then adds members in batches of up to 10,000.
  - **Naming:** the new segment keeps the source title with a suffix naming the source account. Uploads into `attentive_us` add `-CA` (`VIP Customers` → `VIP Customers-CA`); uploads into `attentive_ca` add `-US`.
  - **Duplicate guard:** before creating, the tool looks up the suffixed name in the target and stops with an error if it already exists. Nothing is created or added in that case.
  - **No overwrite, replace or re-upload handling.** This is a one-time migration; the user makes sure each segment is uploaded once. A damaged segment is rebuilt by hand.
  - **Validation:** every row must have the same `segment_name`, or the file is rejected. Emails are lowercased. Phones are normalised to E.164, assuming `+1` when no country code is given. Rows with neither a valid email nor a valid phone go to `<file>.rejected.csv` and aren't sent. Duplicate rows are removed.
  - **Dry-run** validates the CSV and reports the suffixed segment name and the valid, rejected and duplicate counts, without writing anything. `--segment` restricts a run to one segment, for a pilot.
  - **Never subscribes anyone.** The Attentive subscriber migration happens outside this tool and will already be finished before any segment upload. Anyone still not a subscriber in the target is expected to be skipped by Attentive.
- **Job status:** `attentive segments jobs --instance <instance> [--download-results]`. Member jobs run in the background (Attentive targets 4–12 hours). Job IDs are saved in `state/`. The command reports each job's status and, per segment, the skipped count and percentage, and writes skipped rows with Attentive's reason to `<segment>.skipped.csv` so they can be traced back to gaps in the subscriber migration.
- **Authentication:** one custom-app API key per instance, sent as a bearer token, with the `segments:all` scope. `attentive whoami --instance <instance>` calls `/v2/me` so the user can confirm which account a key belongs to before any write.

### Out of scope
Campaigns (with audiences and messages), Lists (with their profiles), Profiles/subscribers, Catalogs and Coupons. The public API has no endpoints to read them. Nothing is built for these: no export, no CSV archive command, no catalog upload-history command.

## Klaviyo

SMS is out of scope: `klaviyo_ca` doesn't use Klaviyo SMS, so no SMS consent is exported or imported.

### Profiles
- **Export:** `klaviyo profiles export --instance <instance> [--segment <id|name>] [--with-predictive] [--resume]` writes every profile, whatever its consent or suppression state, to CSV. It works against either account.
  - Scale (`klaviyo_ca`, 2026-09-23): 635,817 profiles, of which 352,286 are active, 233,995 suppressed and about 49,536 never subscribed. That's about 6,400 pages of 100; the export saves its position after each page.
  - Predictive analytics fields are included only with `--with-predictive`, because they cut Klaviyo's rate limit from 750 to 150 requests per minute.
  - `--segment` exports only that segment's members, in the same layout.
  - "Last order date" is **not** required. It isn't a field on the profile, so it isn't exported or derived.
- **CSV layout:** export and import use the same layout, so an exported file can be edited and re-imported unchanged.
  - Standard fields as columns: `email`, `phone_number`, `external_id`, `first_name`, `last_name`, `locale`, `location.*`, and so on.
  - Custom properties as `properties.<key>`, with nested values stored as JSON text. "Language" isn't a native Klaviyo field; if present it appears here.
  - Email marketing consent as one column per detail: `consent`, `consent_timestamp`, `method`, `method_detail`, `custom_method_detail`, `double_optin`, and suppression details.
- **Import:** `klaviyo profiles import --to <instance> --file <csv> --list-id <id> [--limit N]`. It works in either direction; the project will use it CA → US only.
  - Background: I manually export profiles from both accounts, dedupe them outside this tool (most recent consent timestamp wins), and import only the profiles unique to CA into US. The tool has **no** dedupe features and doesn't check whether profiles already exist. The import updates matching profiles (by email or phone), and the README says so.
  - Consent is written in historical-import mode with the original consent timestamp from the CSV, so no double opt-in and no welcome flows fire. Subscribing requires a list, hence `--list-id`.
  - Unsubscribed or suppressed rows are imported as unsubscribed or suppressed, never subscribed. Never-subscribed rows are imported with no consent change. Our marketing sends go only to subscribed profiles, so they receive no marketing email.
  - Klaviyo accepts only consent status and timestamp, so the original consent method and source are also stored as custom properties `ca_consent_method` and `ca_consent_source`.
  - Suppressed profiles **must** stay suppressed on the destination. The API can't set the original suppression reason, so the reason and date are also stored as custom properties `ca_suppression_reason` and `ca_suppression_timestamp`.
  - `--limit N` imports only the first N rows, for trials.

### Suppressions across accounts
`klaviyo suppressions export --instance klaviyo_ca` and `klaviyo suppressions import --to klaviyo_us --file <csv>` apply every CA email suppression to the destination, **including profiles the external dedupe removed** because they also exist in US. Only hard bounces, spam complaints and manual suppressions are applied, not plain unsubscribes, so the "most recent consent wins" rule still governs unsubscribes.

### Lists
`klaviyo lists export --instance <instance>` writes:
- `lists.csv`: list ID, name, created date, opt-in setting, member count.
- `list_members.csv`: one row per person per list, with the list ID and name, the profile ID, email, and the date they joined.

### Segments
We'll move segments with Klaviyo's "Clone" action in the UI. The tool only records them and their point-in-time membership, to help plan temporary segments on the destination while engagement and activity data is rebuilt.

`klaviyo segments export --instance <instance>` writes:
- `segments.csv`: segment ID, name, created and updated dates, member count, and three yes/no columns plus a list of the event names each segment's rules use:
  - **Engagement:** Klaviyo email events (opened, clicked, received) and order events (Placed Order, Ordered Product).
  - **Site activity:** Klaviyo onsite tracking and Shopify browsing events (Viewed Product, Active on Site, Added to Cart).
  - **Third-party:** any other integration (Eventbrite and the like) or custom events sent through the API.
  - Rules based only on profile properties or list membership get no label. Labels come from each rule's event and that event's source integration.
- `segment_members.csv`: same shape as `list_members.csv`.

### Back in Stock
`klaviyo bis export --instance klaviyo_ca [--since YYYY-MM-DD]` exports signups for upload to STOQ.
- **Source:** Klaviyo has no list endpoint for Back in Stock subscriptions. They exist only as "Subscribed to Back in Stock" events, which are read with the linked profile email (per STOQ's migration guide).
- **Products are identified by SKU, never by variant ID.** STOQ runs only on the US store, and product and variant IDs differ between the stores. The SKU is looked up from the CA variant through Klaviyo's product catalog. The CA variant ID is kept only in a reference column. Rows with no SKU are excluded.
- **Filtering:** keep the latest signup per email and SKU. Skip variants currently in stock, based on the inventory in Klaviyo's catalog; that's CA's stock, so the check is approximate. `--since` drops older signups. Every dropped row goes to `bis.excluded.csv` with the reason.
- **Output:** STOQ's import template columns exactly (`SKU`, `Email`, `Phone`, `Name`, `Market`, `Quantity`, `GDPR confirmed`, `Accepts marketing`, `Language`, `Date`), so the same file can go to `stoq import` or to the STOQ admin upload.

## STOQ

`stoq import --to <stoq_dev|stoq_us> --file <csv> [--resume]`
- Reads a CSV in STOQ's template format; I may edit it by hand first.
- Creates one signup per row through STOQ's v1 intents API (`POST https://app.stoqapp.com/api/v1/intents.json`), identifying the store with the `X-Shopify-Shop-Domain` header. No API key is needed.
- Creating a signup sends nothing to the customer; alerts go out later, on restock.
- Signups that already exist are counted as duplicates, not errors.
- Writes are paced to STOQ's rate limit: 360 points per minute at 2 points per write, so about 180 signups per minute, or about an hour per 10,000.
- The README also documents the no-code alternative: STOQ admin → Back in stock alerts → Settings → Integrations → Import data.

## Testing and acceptance criteria

### Test environments
| Service | Test environment | Consequence |
|---|---|---|
| Attentive | **None. Production only.** | Every write is tried first as a dry-run, then as a one-segment pilot on a small segment, before any bulk upload. |
| Klaviyo | Sandbox requested, not guaranteed | Use `klaviyo_sandbox` if granted. Otherwise, trials run in `klaviyo_us` with `--limit` on addresses we control, subscribed to a dedicated test list. |
| STOQ | Our Shopify dev store (`stoq_dev`) | Trial CSVs must use the dev store's SKUs. |

### Automated tests
Unit tests run against recorded API responses and never call a live account.

### Done when
- **Exports:** row counts in `manifest.json` match the counts shown in the Klaviyo and Attentive dashboards.
- **Klaviyo profile import:** a 10-profile trial (`--limit 10`) using addresses we control. Re-exporting them shows the same consent status and timestamp, `ca_consent_*` properties are set, and a suppressed profile stays suppressed.
- **Klaviyo suppressions:** a 5-row trial appears in the destination's suppression list.
- **Klaviyo segments:** labels are spot-checked by hand against five segments with known rules.
- **Back in Stock export:** a sample of rows checked against the matching Klaviyo events, with correct SKUs, and every excluded row has a reason.
- **STOQ:** a 10-row import into `stoq_dev` appears in STOQ Reports → Current waitlist with the right SKUs, and no customer is messaged.
- **Attentive:** one segment piloted to completion in production. The job results file shows no failures other than expected skips.
- **README:** every command and flag is documented with an example.

## Change history
- 2026-09-23: Spec reviewed and revised. Attentive migration replaced by segment export plus CSV membership upload. Attentive campaigns, lists, profiles, catalogs and coupons dropped. "Last order date" and SMS dropped. Suppressions, SKU matching, the STOQ template format, test environments and acceptance criteria added.
