# Klaviyo, Attentive and STOQ migration tools — requirements

## Context

I'm running a migration, and part of that is merging objects and records from the Canada Klaviyo and Attentive accounts into the corresponding US accounts, plus moving Back in Stock signups into STOQ.

Instance names used throughout:

| Instance | Role |
|---|---|
| `klaviyo_ca`, `attentive_ca` | Source (Canada) |
| `klaviyo_us`, `attentive_us` | Destination (US, the merged store) |
| `klaviyo_sandbox` | Klaviyo test account (account ID `T2aEdf`), for trials |
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
- Secrets live in a git-ignored `.env`, one per instance: `KLAVIYO_CA_API_KEY`, `KLAVIYO_US_API_KEY`, `KLAVIYO_SANDBOX_API_KEY`, `ATTENTIVE_CA_API_KEY`, `ATTENTIVE_US_API_KEY`, `STOQ_DEV_SHOP_DOMAIN`, `STOQ_US_SHOP_DOMAIN`. Keys are never logged.
- Output goes to `./exports/<instance>/<object>/<timestamp>.csv` with a `manifest.json` of record counts per run. Every export is CSV wherever the data fits in columns (nested values as JSON text in a cell). Only an export whose data is too complex for a usable CSV falls back to `.jsonl`, and the README says which ones do.
- All timestamps, in files and on the command line, are UTC ISO 8601 (`2026-09-24T15:30:00Z`).
- A README documents every command and flag, with an example of each, and the STOQ CSV preparation steps.
- Exports hold personal data. I delete `exports/` and `state/` immediately after the migration; the README's run order ends with that step.

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
- Long exports save their position and support `--resume`. Imports and uploads don't resume: a failed import is re-run from the start, which is safe because every write can be repeated.

### Catch-up (delta) run
Immediately before sign-ups are turned off on the CA Klaviyo site, I run a catch-up pass that repeats the migration for anything that changed since the main run:
- Every Klaviyo export used in the migration takes `--since <ISO timestamp>` and returns only records changed since then: profiles by last update, list memberships by join date, suppressions by suppression date, and Back in Stock signups by event date.
- Every write can be repeated safely. Re-importing a profile updates it; adding a profile to a list or suppression it's already in changes nothing; STOQ counts existing signups as duplicates. A catch-up run therefore uses the same commands on the smaller files.
- The catch-up files go through the same external dedupe as the main run.

## Attentive

Attentive's public API (REST and GraphQL) is mostly write-only. The only read endpoints relevant to this project are segment metadata.

### Segments
Uses the Segments (Open Beta) and Bulk Segment Operations (Open Beta) APIs:
[List Segments](https://docs.attentive.com/reference/listsegments), [Create Segment](https://docs.attentive.com/reference/createsegment), [Add Bulk Segment Members](https://docs.attentive.com/reference/postbulksegmentmembers), [Bulk job status](https://docs.attentive.com/reference/getbulkjobstatus). If the beta APIs fail or prove error-prone, we fall back to a manual process in the Attentive UI.

- **No automated CA → US migration.** List Segments returns only `externalId`, `name`, `description`, `created` and `updated`: no rules, counts or members, and there is no endpoint to read members. Segment membership will be requested from our Attentive customer success manager and supplied as CSV files.
- **Export:** `attentive segments export --instance <attentive_ca|attentive_us>` saves all segments (metadata only) to CSV.
- **Upload:** `attentive segments upload --to <instance> --file <csv> [--segment <name>] [--append] [--dry-run]` uploads one CSV per segment, with columns `segment_name, email, phone`.
  - It creates an empty, static segment in the target, then adds members in batches of up to 10,000.
  - **Naming:** the new segment keeps the source title with a suffix naming the source account. Uploads into `attentive_us` add `-CA` (`VIP Customers` → `VIP Customers-CA`); uploads into `attentive_ca` add `-US`.
  - **Duplicate guard:** before creating, the tool looks up the suffixed name in the target. If it already exists and `--append` isn't given, it stops with an error and nothing is created or added.
  - **Re-upload with `--append`:** adds the file's members to the existing suffixed segment instead of creating one, so a segment can be uploaded more than once (for example in the catch-up run). With `--append` and no existing segment, the tool stops with an error. Members are only ever added; nothing is removed or replaced. A damaged segment is rebuilt by hand.
  - **Validation:** the file must have exactly the columns `segment_name, email, phone`; I reformat the CSM's files by hand to match. Every row must have the same `segment_name`, or the file is rejected. Emails are lowercased. Phones are normalised to E.164, assuming `+1` when no country code is given. Rows with neither a valid email nor a valid phone go to `<file>.rejected.csv` and aren't sent. Duplicate rows are removed.
  - **Dry-run** validates the CSV and reports the suffixed segment name, whether it would be created or appended to, and the valid, rejected and duplicate counts, without writing anything. `--segment` restricts a run to one segment, for a pilot.
  - **Never subscribes anyone.** The Attentive subscriber migration happens outside this tool and will already be finished before any segment upload. Anyone still not a subscriber in the target is expected to be skipped by Attentive.
- **Job status:** `attentive segments jobs --instance <instance> [--download-results]`. Member jobs run in the background (Attentive targets 4–12 hours). Job IDs are saved in `state/`. The command reports each job's status and, per segment, the skipped count and percentage, and writes skipped rows with Attentive's reason to `<segment>.skipped.csv` so they can be traced back to gaps in the subscriber migration.
- **Authentication:** one custom-app API key per instance, sent as a bearer token, with the `segments:all` scope. `attentive whoami --instance <instance>` calls `/v2/me` so the user can confirm which account a key belongs to before any write.

### Out of scope
Campaigns (with audiences and messages), Lists (with their profiles), Profiles/subscribers, Catalogs and Coupons. The public API has no endpoints to read them. Nothing is built for these: no export, no CSV archive command, no catalog upload-history command.

## Klaviyo

SMS is out of scope: `klaviyo_ca` doesn't use Klaviyo SMS, so no SMS consent is exported or imported.

Also out of scope: flows, templates, campaigns, and event and order history. None of these are migrated.

`klaviyo whoami --instance <instance>` shows the account ID and name a key belongs to (from `/api/accounts`), so the user can confirm it before any write.

### Profiles
- **Export:** `klaviyo profiles export --instance <instance> [--segment <id|name>] [--since <timestamp>] [--with-predictive] [--resume]` writes every profile, whatever its consent or suppression state, to CSV. It works against either account.
  - Scale (`klaviyo_ca`, 2026-09-23): 635,817 profiles, of which 352,286 are active, 233,995 suppressed and about 49,536 never subscribed. That's about 6,400 pages of 100; the export saves its position after each page.
  - Predictive analytics fields are included only with `--with-predictive`, because they cut Klaviyo's rate limit from 750 to 150 requests per minute.
  - `--segment` exports only that segment's members, in the same layout. If a name matches more than one segment, the tool stops and lists the matching IDs.
  - "Last order date" is **not** required. It isn't a field on the profile, so it isn't exported or derived.
- **CSV layout:** export and import use the same layout, so an exported file can be edited and re-imported unchanged.
  - Standard fields as columns: `email`, `phone_number`, `external_id`, `first_name`, `last_name`, `locale`, `location.*`, and so on.
  - Custom properties as `properties.<key>`, with nested values stored as JSON text. "Language" isn't a native Klaviyo field; if present it appears here.
  - Email marketing consent as one column per detail: `consent`, `consent_timestamp`, `method`, `method_detail`, `custom_method_detail`, `double_optin`, and suppression details.
- **Import:** `klaviyo profiles import --to <instance> --file <csv> --list-id <id> [--limit N]`. It works in either direction; the project will use it CA → US only. It is the first Klaviyo write in the migration: every profile goes into one overall migration list, and the CA lists and segments are recreated or cloned and populated afterwards.
  - Background: I manually export profiles from both accounts, dedupe them outside this tool (most recent consent timestamp wins), and import only the profiles unique to CA into US. The tool has **no** dedupe features and doesn't check whether profiles already exist. The import updates matching profiles (by email or phone), and the README says so.
  - My dedupe also checks that phone numbers are unique against `klaviyo_us` (Klaviyo requires unique phone numbers), and removes custom properties that obviously overlap between the two Shopify stores. The tool imports every remaining `properties.*` column as-is.
  - Consent is written in historical-import mode with the original consent timestamp from the CSV, so no double opt-in and no welcome flows fire. Subscribing requires a list, hence `--list-id`. For the migration this is the overall migration list, a new **LOF Canada Newsletter** list in `klaviyo_us`.
  - Unsubscribed or suppressed rows are imported as unsubscribed or suppressed, never subscribed. Never-subscribed rows are imported with no consent change. Our marketing sends go only to subscribed profiles, so they receive no marketing email. Never-subscribed profiles are imported even though they add to Klaviyo's active-profile billing; that cost is accepted.
  - **Identifiers:** the source Klaviyo profile `id` is never sent. The `external_id` column is written to the custom property `ca_external_id`, not to the destination's `external_id`, so it can't clash with the US Shopify sync. `phone_number` is imported.
  - **Migration tags:** every imported profile, and every profile created by `lists add` or `suppressions import`, gets the custom properties `migrated_from=ca` and `migration_run_id=<run id>`. Existing destination profiles those two commands only update are not tagged, since they aren't CA profiles. The tool generates the run ID for each run and records it in `manifest.json` and the run summary, so a bad batch can be found and segmented or deleted by hand.
  - **Lists:** only the `--list-id` membership is created. Other CA list memberships are recreated afterwards and selectively, with `lists add` or by uploading to existing analogous US lists through the Klaviyo UI.
  - Klaviyo accepts only consent status and timestamp, so the original consent method and source are also stored as custom properties `ca_consent_method` and `ca_consent_source`.
  - Suppressed profiles **must** stay suppressed on the destination. The API can't set the original suppression reason, so the reason and date are also stored as custom properties `ca_suppression_reason` and `ca_suppression_timestamp`.
  - `--limit N` imports only the first N rows, for trials.

### Suppressions across accounts
Suppression is treated as one more place I attach a chosen set of profiles, like a list. The tool doesn't decide which suppressions to apply; I do, by editing the file.
- **Export:** `klaviyo suppressions export --instance <instance> [--since <timestamp>]` writes every email suppression with its email, reason and date.
- **Import:** `klaviyo suppressions import --to <instance> --file <csv>` suppresses every email in the file, **including profiles the external dedupe removed** because they also exist in US. An email with no profile in the destination gets a new, suppressed profile.
- **Suppressions trump everything.** A profile suppressed in CA is suppressed in US even if it subscribed more recently on the US store. The number affected is expected to be very low.
- Can be run more than once; already-suppressed emails are unchanged.

### Lists
`klaviyo lists export --instance <instance> [--since <timestamp>]` writes:
- `lists.csv`: list ID, name, created date, opt-in setting, member count.
- `list_members.csv`: one row per person per list, with the list ID and name, the profile ID, email, and the date they joined. `--since` keeps only memberships that joined after that time.

`klaviyo lists add --to <instance> --list <id> --file <csv>` adds every profile in the file to one list, identified by the `email` column. The file can be just emails (`list_members.csv` filtered by hand) or the full profile layout. I choose which sets of profiles go to which lists; some lists I'll still fill through the Klaviyo UI instead.
- **Missing profiles are created** from whatever profile fields the file has, following the same field rules as `profiles import` (`ca_external_id`, no source `id`).
- **Consent:** if the file has the consent columns, consent is written as in `profiles import` (historical-import mode with the original timestamp, subscribing to this list; unsubscribed and suppressed rows are never subscribed). If it doesn't, consent is unchanged, and adding someone to a list doesn't subscribe them. Almost everyone on the CA lists and segments has consented, so files with consent columns are expected to be the norm.
- It can be run more than once against the same list; existing members are unchanged.
- Klaviyo segments are rule-based and can't have members added directly. To mirror a segment, add its members to a list and build the segment on membership of that list.

### Segments
We'll move segments with Klaviyo's "Clone" action in the UI, which works across accounts. The tool only records them and their point-in-time membership, to help plan temporary segments on the destination while engagement and activity data is rebuilt.

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
- **Products are identified by SKU, never by variant ID.** STOQ runs only on the US store, and product and variant IDs differ between the stores. SKUs are confirmed identical in both stores. The SKU is looked up from the CA variant through Klaviyo's product catalog. The CA variant ID is kept only in a reference column. Rows with no SKU are excluded.
- **Filtering:** keep the latest signup per email and SKU. Skip variants currently in stock, based on the inventory in the `klaviyo_ca` catalog. That's the right check, because STOQ will watch the CA inventory location for CA subscribers. `--since` drops older signups. Every dropped row goes to `bis.excluded.csv` with the reason.
- **Output:** STOQ's import template columns exactly (`SKU`, `Email`, `Phone`, `Name`, `Market`, `Quantity`, `GDPR confirmed`, `Accepts marketing`, `Language`, `Date`), so the same file can go to `stoq import` or to the STOQ admin upload.
  - Filled from Klaviyo where the event or profile has the data: `SKU`, `Email`, `Name` (first and last name), `Language` (the `Language` custom property, if present), `Accepts marketing` (from email consent), `Date` (the event time), and `Quantity` if the event carries it.
  - Everything else is left blank for me to fill by hand: `Market`, `GDPR confirmed`, and anything that points a subscriber at an inventory location.
  - `Phone` is left blank. SMS is out of scope, and a phone number could make STOQ send SMS alerts to people who signed up by email only.

## STOQ

`stoq import --to <stoq_dev|stoq_us> --file <csv>`
- Reads a CSV in STOQ's template format; I may edit it by hand first. The tool only scripts the upload: any inventory-location or market data I put in the CSV is sent as-is, and it doesn't route subscribers itself. (STOQ watches the US inventory location for US and international subscribers and the CA location for CA subscribers.)
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
| Klaviyo | Test account `klaviyo_sandbox` (account ID `T2aEdf`) | Every Klaviyo write is tried there first, on addresses we control, before it touches `klaviyo_us`. |
| STOQ | Our Shopify dev store (`stoq_dev`) | Trial CSVs must use the dev store's SKUs. |

### Automated tests
Unit tests run against recorded API responses and never call a live account.

### Done when
- **Klaviyo profile import:** a 10-profile trial (`--limit 10`) using addresses we control. Re-exporting them shows the same consent status and timestamp, `ca_consent_*` properties are set, and a suppressed profile stays suppressed.
- **Klaviyo suppressions:** a 5-row trial appears in the destination's suppression list, including one profile that was subscribed there.
- **Klaviyo list add:** a 5-row trial adds the profiles to a list without changing their consent; running it again changes nothing.
- **Catch-up run:** in `klaviyo_sandbox`, records changed after a `--since` timestamp are exported and re-imported, and nothing older is included.
- **Klaviyo segments:** labels are spot-checked by hand against five segments with known rules.
- **Back in Stock export:** a sample of rows checked against the matching Klaviyo events, with correct SKUs, and every excluded row has a reason.
- **STOQ:** a 10-row import into `stoq_dev` appears in STOQ Reports → Current waitlist with the right SKUs, and no customer is messaged.
- **Attentive:** one segment piloted to completion in production. The job results file shows no failures other than expected skips. A second upload of the same file is stopped by the duplicate guard, and succeeds with `--append`.
- **README:** every command and flag is documented with an example.

## Legal review
Legal is reviewing the transfer of CA consent into the US account, including CASL implied consent (which expires two years after a purchase) and moving personal data from the CA business to the US one (PIPEDA). The build proceeds as specified; the spec will be updated with their feedback.

## Change history
- 2026-09-23: Second review. Import writes to a new LOF Canada Newsletter list; never-subscribed profiles imported (cost accepted); `external_id` stored as `ca_external_id`; phone numbers imported; `migrated_from` and `migration_run_id` tags added; list recreation and overlapping-property cleanup left to the user. Legal review and open questions sections added.
- 2026-09-23: Spec reviewed and revised. Attentive migration replaced by segment export plus CSV membership upload. Attentive campaigns, lists, profiles, catalogs and coupons dropped. "Last order date" and SMS dropped. Suppressions, SKU matching, the STOQ template format, test environments and acceptance criteria added.
- 2026-09-24: Third review. Klaviyo test account `T2aEdf` confirmed. Catch-up run with `--since` on all Klaviyo exports added. New `klaviyo lists add`. Suppressions are no longer filtered by reason; I choose them, and they override newer US subscribes. Attentive `--append` allows re-uploads. BIS stock check stays on CA stock; STOQ columns filled from Klaviyo where possible, otherwise blank; `Phone` left blank. SKUs, cross-account segment cloning and the CSM CSV format (reformatted by hand) confirmed. Local data deleted after the migration.
- 2026-09-24: Fourth review. Imports don't resume; re-run instead (`--resume` dropped from `stoq import`). `klaviyo whoami` added. CSV everywhere, JSONL only as a fallback. Klaviyo flows, templates, campaigns and event history confirmed out of scope. UTC ISO 8601 timestamps. `lists add` and `suppressions import` create missing profiles; `lists add` writes consent when the file has it. Ambiguous `--segment` names stop with an error.
- 2026-09-24: Export count check against the dashboards dropped (counts change while an export runs). No open questions remain.
