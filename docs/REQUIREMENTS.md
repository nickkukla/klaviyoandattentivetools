# Klaviyo migration tools — requirements

## Context

I'm running a migration, and part of that is merging objects and records from the Canada Klaviyo account into the US account, plus exporting Back in Stock signups for upload to STOQ. Attentive is handled by hand, and the STOQ upload is done in STOQ's admin (see below).

Instance names used throughout:

| Instance | Role |
|---|---|
| `klaviyo_ca` | Source (Canada) |
| `klaviyo_us` | Destination (US, the merged store) |
| `klaviyo_sandbox` | Klaviyo test account (account ID `T2aEdf`), for trials |

The tool is a command-line app that I run directly or ask Claude Code to run from a session. Every command targets a named instance.

API references:
- Klaviyo: https://developers.klaviyo.com/en/reference/api_overview
- STOQ: https://docs.stoqapp.com/ and https://help.stoqapp.com/back-in-stock/migrate-klaviyo-back-in-stock-signups/

## General

### Runtime and configuration
- Python 3.12, `uv`, `typer` CLI. Runs locally by hand (WSL). No scheduler, no server.
- Secrets live in a git-ignored `.env`, one per instance: `KLAVIYO_CA_API_KEY`, `KLAVIYO_US_API_KEY`, `KLAVIYO_SANDBOX_API_KEY`. Keys are never logged.
- Output goes to `./exports/<instance>/<object>/<timestamp>.csv` with a `manifest.json` of record counts per run. Every export is CSV wherever the data fits in columns (nested values as JSON text in a cell). Only an export whose data is too complex for a usable CSV falls back to `.jsonl`, and the README says which ones do.
- All timestamps, in files and on the command line, are UTC ISO 8601 (`2026-09-24T15:30:00Z`).
- A README documents every command and flag, with an example of each, and the STOQ CSV preparation and admin upload steps.
- Exports hold personal data. I delete `exports/` and `state/` immediately after the migration; the README's run order ends with that step.

### Write safety
- Every write command names its target with `--to <instance>`. Before writing it prints the account name, the target instance and the record count, and asks the user to type the instance name to confirm.
- `--yes` skips the prompt for scripted runs, including runs started by Claude.
- Writes into a `_ca` instance also require `--allow-write-to-source`.
- There is no dry-run mode. Every command is either an export (for archiving, or to check or edit data) or an import of a previously exported file, and Klaviyo writes are tried in `klaviyo_sandbox` first.

### Errors and retries
- Rate-limit (429) and server (5xx) errors are retried with increasing waits, honouring the service's `Retry-After`, up to 6 attempts.
- Per-record errors go to `<run>.errors.csv` and the run continues.
- Asynchronous bulk jobs (Klaviyo imports) are tracked, and their per-record errors go to the same errors file.
- Every run ends with a summary of counts (read, written, skipped, failed) and exits non-zero if anything failed.
- Long exports save their position and support `--resume`. Imports don't resume: a failed import is re-run from the start, which is safe because every write can be repeated.

### Catch-up (delta) run
Immediately before sign-ups are turned off on the CA Klaviyo site, I run a catch-up pass that repeats the migration for anything that changed since the main run:
- Every Klaviyo export used in the migration takes `--since <ISO timestamp>` and returns only records changed since then: profiles by last update, list memberships by join date, suppressions by suppression date, and Back in Stock signups by event date. Unsubscribes don't change a profile's last-update time, so they're caught only by the suppressions export, which every catch-up run must include.
- Every write can be repeated safely. Re-importing a profile updates it; adding a profile to a list or suppression it's already in changes nothing. The Back in Stock catch-up file only holds signups after `--since`, so earlier ones aren't uploaded again. A catch-up run therefore uses the same commands on the smaller files.
- The catch-up files go through the same external dedupe as the main run.

## Attentive

Out of scope: Attentive segments are handled by hand in the Attentive UI. The tool has no Attentive commands.

Why: the Segments API (Open Beta) only lists segments created through the API. In phase 1, `GET /v2/segments` returned an empty list in both accounts, which both have UI segments, so existing segments can't be listed, and members can't be read at all. Attentive campaigns, lists, subscribers, catalogs and coupons were already out of scope (no read endpoints).

## Klaviyo

SMS is out of scope: `klaviyo_ca` doesn't use Klaviyo SMS, so no SMS consent is exported or imported.

Also out of scope: flows, templates, campaigns, and event and order history. None of these are migrated.

`klaviyo whoami --instance <instance>` shows the account ID and name a key belongs to (from `/api/accounts`), so the user can confirm it before any write.

### Profiles
- **Export:** `klaviyo profiles export --instance <instance> [--segment <id|name>] [--since <timestamp>] [--with-predictive] [--resume]` writes every profile, whatever its consent or suppression state, to CSV. It works against either account.
  - Scale (`klaviyo_ca`, full export 2026-09-24): 291,337 profiles: 164,798 active (have an email and aren't suppressed, matching the dashboard), 106,110 suppressed, and 20,429 with no email address (707 with neither email nor phone). By consent: 90,180 subscribed, 98,463 unsubscribed, 102,694 never subscribed. That's about 2,900 pages of 100 (about 40 minutes); the export saves its position after each page. (`klaviyo_us`, 2026-09-23 dashboard: 635,817 profiles, 352,286 active, 233,995 suppressed, about 49,536 never subscribed.)
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
  - Rows without an email are skipped and listed with the reason in `<run>.skipped.csv` (20,429 CA profiles have no email; SMS is out of scope). Repeated emails in a file are skipped after the first.
  - Klaviyo keeps the existing consent timestamp of a profile that is already subscribed in the destination; only profiles not currently subscribed get the CA timestamp. Unsubscribes are timestamped at import time (the API can't backdate them); the original date is in `ca_suppression_timestamp`.
  - `$`-prefixed properties (Klaviyo-internal, e.g. `$consent`) are never written. Blank cells never clear destination values.

### Suppressions across accounts
Suppression is treated as one more place I attach a chosen set of profiles, like a list. The tool doesn't decide which suppressions to apply; I do, by editing the file.
- **Export:** `klaviyo suppressions export --instance <instance> [--since <timestamp>]` writes every email suppression with its email, reason and date.
- **Import:** `klaviyo suppressions import --to <instance> --file <csv> [--limit N] [--as-unsubscribe]` suppresses every email in the file, **including profiles the external dedupe removed** because they also exist in US. An email with no profile in the destination gets a new, suppressed profile.
- **Suppressions trump everything.** A profile suppressed in CA is suppressed in US even if it subscribed more recently on the US store. The number affected is expected to be very low.
- Can be run more than once; already-suppressed emails are unchanged.
- Klaviyo applies suppression jobs in the background: in `klaviyo_sandbox` they took about four hours, and the job status stayed `processing` and reported every profile as skipped even after they were suppressed. So the import submits the jobs and doesn't wait for them.
- **Check:** `klaviyo suppressions check --instance <instance> --file <csv>` (read-only) reports each email's current state (suppressed, unsubscribed only, not suppressed, no profile) and writes those not yet suppressed to a CSV. Run it some hours after the import.
- `--limit N` sends only the first N rows. The migration pilots the suppression endpoint on a single address (`--limit 1`) and confirms it with `suppressions check` before sending the full file.
- `--as-unsubscribe` (also on `profiles import`) unsubscribes instead of suppressing. It blocks marketing email immediately, without waiting hours for the suppression jobs, but a later subscribe on the US store lifts it and the Klaviyo reason reads "Unsubscribed" (the original is kept in `ca_suppression_reason`). Use it as a floor, then apply true suppression in the Klaviyo UI.

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
  - **Site activity:** Klaviyo onsite tracking and Shopify browsing events (Viewed Product, Active on Site, Added to Cart, Checkout Started), whatever integration sends them (in our accounts some arrive through the API).
  - **Third-party:** any other integration (Eventbrite and the like) or custom events sent through the API.
  - Rules based only on profile properties or list membership get no label, and neither do Klaviyo subscription events (Subscribed to List, Subscribed to / Unsubscribed from Email Marketing, Subscribed to Back in Stock). Labels come from each rule's event and that event's source integration.
- `segment_members.csv`: same shape as `list_members.csv`.

### Back in Stock
`klaviyo bis export --instance klaviyo_ca [--since YYYY-MM-DD]` exports signups for upload to STOQ. Scale (`klaviyo_ca`, 2026-09-24): 43,445 signup events since 2021-01-11, all with a SKU and a linked profile.
- **Source:** Klaviyo has no list endpoint for Back in Stock subscriptions. They exist only as "Subscribed to Back in Stock" events, which are read with the linked profile email (per STOQ's migration guide).
- **Products are identified by SKU, never by variant ID.** STOQ runs only on the US store, and product and variant IDs differ between the stores. SKUs are confirmed identical in both stores. The SKU is read from the event's `SKU` property (Klaviyo's API doesn't expose Shopify-synced catalogs, so there is no catalog lookup). The CA variant and product IDs are kept only in a separate `bis.reference.csv` (with product and variant names and the Klaviyo event ID), so the upload file stays exactly STOQ's template. Rows with no SKU are excluded.
- **Filtering:** keep the latest signup per email and SKU. There is no stock check: every signup is exported, including ones for variants now in stock, and STOQ handles them. `--since` reads only signups after that date (earlier ones aren't read at all). Every row dropped from the signups read goes to `bis.excluded.csv` with the reason (older signup for the same email and SKU, profile has no email, event has no SKU).
- **Output:** STOQ's import template columns exactly (`SKU`, `Email`, `Phone`, `Name`, `Market`, `Quantity`, `GDPR confirmed`, `Accepts marketing`, `Language`, `Date`), so the file can be uploaded as-is in STOQ admin.
  - Filled from Klaviyo where the event or profile has the data: `SKU`, `Email`, `Name` (first and last name), `Language` (a `Language` custom property if present, otherwise the profile's locale reduced to its language code, `en-CA` → `en`; no CA profile has the property), `Accepts marketing` (`true` if the profile is currently subscribed to email and not suppressed, otherwise `false`), and `Date` (the event time, as `dd/mm/yyyy` in UTC, one of the two formats STOQ accepts). The events carry no quantity, so `Quantity` is blank (STOQ defaults it to 1).
  - Everything else is left blank for me to fill by hand: `Market`, `GDPR confirmed`, and anything that points a subscriber at an inventory location.
  - `Phone` is left blank. SMS is out of scope, and a phone number could make STOQ send SMS alerts to people who signed up by email only.

## STOQ

The tool doesn't write to STOQ. I upload the `klaviyo bis export` file by hand in STOQ admin → Back in stock alerts → Settings → Integrations → Import data, after filling `Market`, `GDPR confirmed` and any inventory-location data. The README documents the steps.

Why: STOQ's v1 intents API needs the US store's Shopify variant and product IDs rather than a SKU, takes the market as a numeric Shopify Market ID, and has no inventory-location field. The admin import takes the template columns, SKU included.

## Testing and acceptance criteria

### Test environments
| Service | Test environment | Consequence |
|---|---|---|
| Klaviyo | Test account `klaviyo_sandbox` (account ID `T2aEdf`) | Every Klaviyo write is tried there first, on addresses we control, before it touches `klaviyo_us`. |
| STOQ | Our Shopify dev store | A trial CSV using the dev store's SKUs is uploaded by hand in its STOQ admin. |

### Automated tests
Unit tests run against recorded API responses and never call a live account.

### Done when
- **Klaviyo profile import:** a 10-profile trial (`--limit 10`) using addresses we control. Re-exporting them shows the same consent status and timestamp, `ca_consent_*` properties are set, and a suppressed profile stays suppressed.
- **Klaviyo suppressions:** a 5-row trial appears in the destination's suppression list, including one profile that was subscribed there.
- **Klaviyo list add:** a 5-row trial adds the profiles to a list without changing their consent; running it again changes nothing.
- **Catch-up run:** in `klaviyo_sandbox`, records changed after a `--since` timestamp are exported and re-imported, and nothing older is included.
- **Klaviyo segments:** labels are spot-checked by hand against five segments with known rules.
- **Back in Stock export:** a sample of rows checked against the matching Klaviyo events, with correct SKUs, and every excluded row has a reason.
- **STOQ:** a 10-row export file, edited to the dev store's SKUs and uploaded in the dev store's STOQ admin, appears in STOQ Reports → Current waitlist with the right SKUs, and no customer is messaged.
- **README:** every command and flag is documented with an example.

## Legal review
Legal is reviewing the transfer of CA consent into the US account, including CASL implied consent (which expires two years after a purchase) and moving personal data from the CA business to the US one (PIPEDA). The build proceeds as specified; the spec will be updated with their feedback.

## Change history
- 2026-09-23: Second review. Import writes to a new LOF Canada Newsletter list; never-subscribed profiles imported (cost accepted); `external_id` stored as `ca_external_id`; phone numbers imported; `migrated_from` and `migration_run_id` tags added; list recreation and overlapping-property cleanup left to the user. Legal review and open questions sections added.
- 2026-09-23: Spec reviewed and revised. Attentive migration replaced by segment export plus CSV membership upload. Attentive campaigns, lists, profiles, catalogs and coupons dropped. "Last order date" and SMS dropped. Suppressions, SKU matching, the STOQ template format, test environments and acceptance criteria added.
- 2026-09-24: Third review. Klaviyo test account `T2aEdf` confirmed. Catch-up run with `--since` on all Klaviyo exports added. New `klaviyo lists add`. Suppressions are no longer filtered by reason; I choose them, and they override newer US subscribes. Attentive `--append` allows re-uploads. BIS stock check stays on CA stock; STOQ columns filled from Klaviyo where possible, otherwise blank; `Phone` left blank. SKUs, cross-account segment cloning and the CSM CSV format (reformatted by hand) confirmed. Local data deleted after the migration.
- 2026-09-24: Fourth review. Imports don't resume; re-run instead (`--resume` dropped from `stoq import`). `klaviyo whoami` added. CSV everywhere, JSONL only as a fallback. Klaviyo flows, templates, campaigns and event history confirmed out of scope. UTC ISO 8601 timestamps. `lists add` and `suppressions import` create missing profiles; `lists add` writes consent when the file has it. Ambiguous `--segment` names stop with an error.
- 2026-09-24: Export count check against the dashboards dropped (counts change while an export runs). No open questions remain.
- 2026-09-24: Phase 1 finding. Klaviyo's API doesn't expose Shopify-synced catalogs, so the BIS SKU comes from the event's `SKU` property and the in-stock check is dropped; STOQ handles in-stock variants.
- 2026-09-24: Phase 1 finding. Segment labels: site activity is decided by event name whatever the integration, and includes Checkout Started; Klaviyo subscription events get no label.
- 2026-09-24: Phase 1 finding. Attentive's List Segments API only sees API-created segments. Attentive dropped from the tool entirely (segment export, upload, jobs, whoami, instances and keys); segments are handled by hand.
- 2026-09-24: Phase 1 finding. STOQ's v1 intents API needs US Shopify variant and product IDs, not SKUs. `stoq import`, the `stoq_dev`/`stoq_us` instances and their `.env` variables dropped; the BIS export file is uploaded by hand in STOQ admin.
- 2026-09-24: Phase 2. The profile counts previously given for `klaviyo_ca` were `klaviyo_us` figures; CA figures replaced with the full export's counts.
- 2026-09-24: Phase 3. Rows without an email are skipped. `suppressions import` gets `--limit` for a one-address pilot, and both suppression paths get `--as-unsubscribe` as a fallback. No writes to `klaviyo_ca` or `klaviyo_us` during development.
- 2026-09-24: Phase 3. Bulk suppression does work in the sandbox but applied about four hours after submission, with a misleading job status; the import no longer waits on suppression jobs, and `suppressions check` confirms the result.
- 2026-09-24: Phase 4. BIS `Language` falls back to the profile locale; `Accepts marketing` means subscribed and not suppressed; `Date` is `dd/mm/yyyy` (STOQ's format); CA IDs move to a separate `bis.reference.csv` so the upload file is exactly the template.
