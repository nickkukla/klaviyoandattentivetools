# migtool

Command-line tools for moving the Canada Klaviyo account (`klaviyo_ca`) into the US account (`klaviyo_us`), and for exporting Klaviyo Back in Stock signups for upload to STOQ.

- What the tools do and why: `docs/REQUIREMENTS.md`
- Build phases and the migration run order: `docs/BUILD_PLAN.md`
- What the Klaviyo and STOQ APIs actually do (tested): `docs/API_NOTES.md`
- Acceptance criteria and their results: `docs/SIGNOFF.md`
- Importing the dedupe files (the main migration run): `docs/DEDUPE_IMPORT.md`

Attentive segments and the STOQ upload are done by hand. The tool has no Attentive commands and never writes to STOQ.

## Warnings

Read these before running anything that writes.

- **`profiles import` overwrites matching profiles.** A CA row whose email or phone matches an existing US profile updates it. Dedupe the CA and US exports first (outside this tool), and import only profiles unique to CA.
- **Suppressions override newer US subscribes.** `suppressions import` suppresses every email in the file, including people who subscribed more recently on the US store. Edit the file to choose which suppressions to apply.
- **Suppression jobs are slow, and their status can't be trusted.** In the sandbox, Klaviyo applied bulk suppressions two to four hours after submission. The job status stayed `processing` and reported every profile as skipped. Confirm with `klaviyo suppressions check`, not the job status, and pilot one address first.
- **Uploading to STOQ can change Klaviyo consent.** In the dev store, STOQ's Klaviyo integration subscribed uploaded signups to email marketing (including some marked `Accepts marketing = false`, some previously unsubscribed, and one suppressed profile). Before uploading, remove BIS rows for people who are unsubscribed or never subscribed (see [STOQ](#stoq-preparing-and-uploading-the-back-in-stock-file)).
- **`Phone` is left blank in the STOQ file on purpose.** SMS is out of scope, and a phone number could make STOQ send SMS alerts to people who signed up by email only.
- **Exports hold personal data.** Delete them as soon as the migration is finished, except the kept snapshot in `exports/og_exports/` (see [Deleting local data](#deleting-local-data)).
- **Nothing is written to `klaviyo_ca`** unless you pass `--allow-write-to-source`. The migration never needs it.
- **Each instance is tied to its Klaviyo account** (`klaviyo_ca` = `Ka6Lvr`, `klaviyo_us` = `KF4XLe`, `klaviyo_sandbox` = `T2aEdf`). Every command checks the key's account first and stops if it doesn't match, so a key in the wrong `.env` variable can't be used as the wrong account.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv).

```
uv sync
cp .env.example .env      # then fill in the keys
uv run migtool instances  # shows which keys are set (never their values)
```

Run every command from the repository root, since `.env` and the `exports/` and `state/` folders are relative to it.

### Instances and keys

| Instance | `.env` variable | Account | Key scopes |
|---|---|---|---|
| `klaviyo_ca` | `KLAVIYO_CA_API_KEY` | `Ka6Lvr`, Left On Friday Canada (source) | Read only: `accounts:read`, `profiles:read`, `lists:read`, `segments:read`, `metrics:read`, `events:read` |
| `klaviyo_us` | `KLAVIYO_US_API_KEY` | `KF4XLe`, Left On Friday (destination) | The read scopes, plus `profiles:write`, `subscriptions:write`, `lists:write`. **Keep it read-only until the migration run.** |
| `klaviyo_sandbox` | `KLAVIYO_SANDBOX_API_KEY` | `T2aEdf`, Left In Friday Dev (test account) | Read and write scopes as for `klaviyo_us` |
| `shopify_us` | `SHOPIFY_US_ACCESS_TOKEN`, with the store in `SHOPIFY_US_SHOP` | The US store's `*.myshopify.com` domain | Read only: `read_customers`, `read_orders` |
| `shopify_ca` | `SHOPIFY_CA_ACCESS_TOKEN`, with the store in `SHOPIFY_CA_SHOP` | The CA store's `*.myshopify.com` domain | Read only: `read_customers`, `read_orders` |

Keys never appear in output, logs or error messages. Check a key before using it:

```
uv run migtool klaviyo whoami --instance klaviyo_us
```

Run the tests with `uv run pytest`. They use recorded responses and never call a live account.

## How writes are protected

Every write command (`profiles import`, `profiles set-property`, `suppressions import`, `lists add`, `lists copy`, `segments copy`):

1. names its target with `--to <instance>`;
2. prints the account name and ID, the instance and the number of records, and asks you to type the instance name to confirm (`--yes` skips this, for scripted runs);
3. refuses to write to a `_ca` instance unless `--allow-write-to-source` is given, even with `--yes`.

Every write can be repeated safely. Re-importing a profile updates it, adding a profile to a list it's already on changes nothing, and suppressing a suppressed email changes nothing. A failed import is simply re-run; imports don't resume.

When a write goes wrong partway:

- **Bad rows:** Klaviyo refuses a whole batch if one row is invalid (a malformed email, say), and says which row. The tool drops that row, resends the rest, and lists refused rows with Klaviyo's reason in the errors file. A profile over Klaviyo's 100 KB per-profile limit is refused before sending.
- **Bad settings:** an error about the request itself rather than a row (for example a `--list-id` that doesn't exist) stops the run straight away, recorded as `aborted`. Fix the option and re-run.
- **Unknown outcomes:** counts come from Klaviyo's own job results. A profile whose result can't be confirmed (job still running, failed, or its error list unreadable) is counted as failed with "outcome unknown", never as written.
- **Retried writes:** if a write fails in a way that means Klaviyo may already have received it (a lost response, a server error), it's retried with a warning and **recorded in `state/<instance>/ambiguous_writes.json`**, and so is one whose last attempt fails that way. Repeating the same write is safe, but a delayed first copy could land after a *later, different* write (say, re-setting a hold after 05 released it). So **the next write command stops** until you've waited a few minutes and confirmed the earlier file with `dedupe check`; then re-run it with `--retries-settled`, which clears the record.
- **Unfinished jobs:** before writing, every write command looks up the profile import jobs earlier runs saved in `state/<instance>/jobs.json` and records their final status (or "not found" once Klaviyo has dropped a job, after seven days), so each is looked up once. If one is still processing (a run stopped with Ctrl-C, say, while its job ran on), the command stops until it finishes, because Klaviyo doesn't guarantee the order jobs apply in.
- **Creating a list** (`lists copy --create`) is never retried after a failure that may have reached Klaviyo, since a retry would make a second list. The tool looks the list up by name instead, and stops if it can't tell which one to use.
- **Aborted runs:** if the run stops (a revoked key, an outage after retries, Ctrl-C), it still writes the manifest (status `aborted`), the errors and skipped files and the summary, and exits non-zero. Jobs already submitted are listed in `state/` and may still be applied.

## Output files

Files are UTF-8. The tool also reads CSVs saved by Excel as "CSV UTF-8" (with a byte-order mark).

**Opening exports in Excel:** customer-entered text (names, properties) can start with `=`, `+`, `-` or `@`, which Excel may treat as a formula. Use Data → From Text/CSV and set the columns to Text, or edit the files in a text editor, rather than double-clicking them. The tool doesn't escape these values, because escaping would change the data on re-import.

### Property types

`profiles export` keeps each custom property's type in its column header, so re-importing restores it exactly:

| Header | Values | Imported as |
|---|---|---|
| `properties.zip` (no suffix) | anything | text, always (`01234` and `123` stay text) |
| `properties.orders#number` | `3`, `2.5` | number |
| `properties.vip#bool` | `true`, `false` | true/false |
| `properties.tags#json` | `["vip","swim"]`, `"x"`, `7` | JSON (lists, objects, and properties whose type varies between profiles) |

Keep the suffixes when editing. A column you add without a suffix is imported as text. A text property whose own name ends in a suffix is exported with an extra `#text` (`properties.code#number#text` is the text property `code#number`), so it can't clash with a typed column. Whole numbers are restored exactly, however large. A cell that doesn't fit its column's type (`three`, `NaN` or `inf` in a `#number` column, or anything but strict JSON, such as `{"score":NaN}`, in a `#json` column) is reported in the errors file and that row isn't sent. Exports made before this change have no suffixes, so re-export before importing.

Everything goes under `exports/<instance>/<object>/`, named by the run's UTC start time (for example `20260924T195337Z.csv`):

- `manifest.json`: one entry per run, with row counts, options, and for imports the `migration_run_id`, the source file and the per-step counts.
- `<run>.errors.csv`: per-record errors. The run carries on, and exits non-zero if there were any.
- `<run>.skipped.csv` (imports): rows not sent, with the reason (for example no email).

Every run ends with a summary (read, written, skipped, failed). Timestamps in files and on the command line are UTC ISO 8601 (`2026-09-24T15:30:00Z`). The one exception is the STOQ file's `Date` column, which uses STOQ's `dd/mm/yyyy`.

`state/` holds checkpoints for `--resume` and the IDs of Klaviyo bulk jobs.

## Commands

### `migtool instances`

Lists every instance and whether its `.env` variable is set.

```
uv run migtool instances
```

### `migtool klaviyo whoami`

Shows the account ID and name a key belongs to. Use it before any write.

| Flag | |
|---|---|
| `--instance` (required) | Instance to check |

```
uv run migtool klaviyo whoami --instance klaviyo_us
```

### `migtool klaviyo profiles export`

Writes every profile, whatever its consent or suppression state, to CSV. The layout is the one `profiles import` reads, so an exported file can be edited and re-imported.

Columns: `id`, `email`, `phone_number`, `external_id`, names, `locale`, dates, `location.*`, email consent (`consent`, `consent_timestamp`, `method`, `method_detail`, `custom_method_detail`, `double_optin`, …), suppression (`suppression_reason`, `suppression_timestamp`, all `suppressions` as JSON), custom properties as `properties.<key>` (see [Property types](#property-types)), and with `--with-predictive`, `predictive_analytics.*`.

| Flag | |
|---|---|
| `--instance` (required) | Instance to export from |
| `--segment` | Only that segment's members, by ID or exact name. An ambiguous name stops with the matching IDs. |
| `--since` | Only profiles updated after this time. **Unsubscribes don't change a profile's update time**, so a catch-up run also needs `suppressions export --since`. |
| `--with-predictive` | Add predictive analytics columns (Klaviyo's rate limit drops from 700 to 150 requests per minute) |
| `--resume` | Continue the unfinished export with the same options. The export saves its position after every 100 profiles. |

```
uv run migtool klaviyo profiles export --instance klaviyo_ca
uv run migtool klaviyo profiles export --instance klaviyo_ca --resume
uv run migtool klaviyo profiles export --instance klaviyo_ca --since 2026-10-01T00:00:00Z
uv run migtool klaviyo profiles export --instance klaviyo_ca --segment "VIP Customers" --with-predictive
```

A full `klaviyo_ca` export (291,337 profiles) takes about 40 minutes. Klaviyo can return a record twice if it changes during the export; the export keeps the last copy and records `duplicates_dropped` in the manifest.

### `migtool klaviyo profiles import`

Imports a `profiles export` CSV (edited as needed) into the destination, keeping consent and suppression:

1. Bulk-imports every row's fields into `--list-id`, with `ca_external_id`, `ca_consent_method`, `ca_consent_source`, `ca_suppression_reason`, `ca_suppression_timestamp`, `migrated_from=ca` and `migration_run_id`. The source `id`, `external_id` and `$…` properties are never sent. Blank cells never clear a destination value.
2. Subscribes `SUBSCRIBED` rows in historical-import mode with their original consent timestamp. There's no double opt-in and no list flows. A profile that's already subscribed in the destination keeps its existing timestamp.
3. Unsubscribes `UNSUBSCRIBED` rows. Klaviyo stamps the unsubscribe with the import time; the original date is in `ca_suppression_timestamp`.
4. Suppresses rows with any other suppression reason (hard bounce, spam complaint, user-suppressed, invalid email). The jobs are submitted and not waited on; see `suppressions check`.

Never-subscribed rows get no consent change. Rows without an email are skipped and listed in the skipped file.

Klaviyo rules to expect in the errors file or the data:

- A subscribe backdated to before a **newer unsubscribe** in the destination is refused ("backdated consent date … is before current unsubscription date"). That person stays unsubscribed, which is the right outcome.
- An **invalid phone number** is silently dropped on import, with no error. Validate phone numbers during the dedupe.

Steps 2–4 only run for profiles whose import Klaviyo **confirmed**. If a profile's import failed, was refused or is still pending, its consent is held back (so a subscribe can never create a bare, untagged profile), the row is in the errors file, and the manifest counts it under `steps.consent_held_back`. Fix the rows and re-run them.

A row with a hard bounce or spam complaint in its `suppressions` history is suppressed even if its latest suppression is an unsubscribe.

**Flows:** step 1 adds profiles to the list before step 2's historical subscribe, so a flow triggered by joining that list could fire. For the migration, every destination flow is gated on profile triggers that exclude CA members, so no flow runs for them.

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--file` (required) | CSV to import |
| `--list-id` (required) | List every imported profile joins, and that subscribed rows subscribe to (for the migration: the LOF Canada Newsletter list in `klaviyo_us`) |
| `--limit` | Only the first N rows, for trials |
| `--as-unsubscribe` | Unsubscribe instead of suppressing in step 4 (see `suppressions import`) |
| `--yes` | Skip the typed confirmation |
| `--allow-write-to-source` | Allow writing to a `_ca` instance |
| `--retries-settled` | Continue after earlier writes were retried following a lost response (see [How writes are protected](#how-writes-are-protected)) |

```
uv run migtool klaviyo profiles import --to klaviyo_sandbox --file trial.csv --list-id TQ9jRX --limit 10
uv run migtool klaviyo profiles import --to klaviyo_us --file ca_unique_profiles.csv --list-id <LOF Canada Newsletter list ID>
uv run migtool klaviyo profiles import --to klaviyo_us --file ca_unique_profiles.csv --list-id <ID> --as-unsubscribe
uv run migtool klaviyo profiles import --to klaviyo_sandbox --file trial.csv --list-id TQ9jRX --yes   # no prompt
uv run migtool klaviyo profiles import --to klaviyo_us --file ca_unique_profiles.csv --list-id <ID> --retries-settled
# Writing back into the CA account is never needed for the migration, and is refused without this flag:
uv run migtool klaviyo profiles import --to klaviyo_ca --file fix.csv --list-id <ID> --allow-write-to-source
```

The run's `migration_run_id` is printed and saved in the manifest. Every imported profile carries it, so a bad batch can be found and segmented or deleted in Klaviyo.

### `migtool klaviyo profiles set-property`

Sets one custom property on existing profiles listed in a CSV with an `email` column, for holds such as `catchup_hold=true`. A flow profile filter `catchup_hold equals false` then skips those profiles, while profiles without the property still pass.

That is how flow profile filters behaved in `klaviyo_us` on 2026-09-27, when live test orders were run under `migration_hold equals false`. Customers without the property got Order Confirmation, and a held customer didn't. Klaviyo documents the opposite for *segment* conditions (an unset property doesn't match a Boolean-false condition), so don't reuse the rule in a segment. For a new property, confirm it on a flow with a test order from a profile that doesn't have the property before relying on it.

- Only the property is sent. Consent, lists and other fields are untouched.
- Only existing profiles are sent. Emails with no profile are listed in the skipped file and not created. The existence check runs just before the write, but the bulk import is an upsert: a profile deleted, or whose email changed, in between would be re-created carrying only the property. `check-property` shows every profile as it stands.
- Before the confirmation, the command looks up which emails have a profile, so the count you confirm is the count it writes.
- `--type` is `bool` (the default), `number` or `text`. To release a hold, run it again with `--value false`.
- Klaviyo takes a few minutes to apply the import. Run `profiles check-property` before relying on the property, for example before an order import.

```
uv run migtool klaviyo profiles set-property --to klaviyo_sandbox --file hold.csv --key catchup_hold --value true --limit 5
uv run migtool klaviyo profiles set-property --to klaviyo_us --file hold.csv --key catchup_hold --value true
```

### `migtool klaviyo profiles check-property`

Read-only. Reads every email in the CSV back from Klaviyo and checks that the property holds the value, type for type (`true` stored as text doesn't count as `true`). Profiles that are missing, or that have a different or unset value, go to `<run>.mismatches.csv`. Rows it can't check (no email, invalid email) go to `<run>.skipped.csv`; repeated emails are checked once. The command exits with code 1 on any mismatch, any row it couldn't check, or when nothing was checked. It takes the same `--key`, `--value` and `--type` as `set-property`.

Emails that `set-property` skipped for having no profile show as "no profile" here, so a run over the same file then exits with code 1. The run is clean when `mismatched` is 0 and `no profile` matches `set-property`'s skipped count.

```
uv run migtool klaviyo profiles check-property --instance klaviyo_us --file hold.csv --key catchup_hold --value true
```

### `migtool klaviyo dedupe import`

Imports one of the dedupe files (Klaviyo UI-import layout) under the rules of its role. **The file-by-file rules, list IDs and full command sequence are in `docs/DEDUPE_IMPORT.md`.**

| Role | Files | Behaviour |
|---|---|---|
| `hold` | 01, 05 | Updates existing profiles only. Sends `migration_hold` and nothing else. No list, no consent, no tags. |
| `hold-new` | 01b | Creates or updates. `migration_hold` plus migration tags. No list, no consent. |
| `suppress` | 02 | Rows that were US profiles before the migration (per `--us-snapshot`) join `--join-list` and get `ca_suppression_*`. The rest are CA-only: created or updated and tagged. Then all are submitted for suppression. |
| `new` | 03a–03d | Creates or updates, joins `--join-list`, adds tags. Consent from `Email Marketing Consent`: `Subscribe` → historical subscribe to `--subscribe-list`; `Unsubscribed` → unsubscribe. |
| `kept` | 04a, 04b | Updates existing profiles only, joins `--join-list`, adds tags. Consent as for `new`, using `ca_consent_timestamp`. |
| `consent` | consent sync D14 | Existing profiles only; the profile write sends the email and nothing else (other columns, such as `shopify_customer_id`, are never written). Subscribes them to `--subscribe-list` with the file's date (Shopify's consent date) and the custom source "Shopify email consent (consent sync)". No join list, no tags. |

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--file` (required) | The dedupe CSV |
| `--role` (required) | `hold`, `hold-new`, `suppress`, `new`, `kept` or `consent` |
| `--join-list` | List the profiles join (required for `suppress`, `new`, `kept`; refused for the others) |
| `--subscribe-list` | List `Subscribe` rows subscribe to (roles `new`, `kept`; required when the file has `Subscribe` rows) |
| `--types-from` | A `profiles export` CSV whose header gives each custom property's type (required for `new`) |
| `--us-snapshot` | The pre-migration US `profiles export` CSV; decides which 02 rows are existing US profiles (required for `suppress`) |
| `--limit` | Only the first N rows, for a pilot |
| `--yes` | Skip the typed confirmation |
| `--allow-write-to-source` | Allow writing to a `_ca` instance |
| `--retries-settled` | Continue after earlier writes were retried following a lost response (see [How writes are protected](#how-writes-are-protected)) |

```
uv run migtool klaviyo dedupe import --to klaviyo_us --role hold --file dedupe/exports/01_hold_only.csv
uv run migtool klaviyo dedupe import --to klaviyo_us --role new --file dedupe/exports/03a_new_subscribed.csv --join-list T7TTAp --subscribe-list XrGL9u --types-from exports/mainrun/klaviyo_ca/profiles/20260925T151451Z.csv --limit 5
uv run migtool klaviyo dedupe import --to klaviyo_sandbox --role kept --file trial_04a.csv --join-list Sc9zHg --subscribe-list XrGL9u --yes
uv run migtool klaviyo dedupe import --to klaviyo_us --role suppress --file dedupe/exports/02_suppressions.csv --join-list Sc9zHg --us-snapshot exports/mainrun/klaviyo_us/profiles/20260925T153516Z.csv --retries-settled
uv run migtool klaviyo dedupe import --to klaviyo_ca --role hold --file fix.csv --allow-write-to-source   # never needed for the migration
```

Before sending anything, it works out and shows the plan: rows to send (existing and new), rows skipped or unreadable, lists by name, and subscribe and unsubscribe counts.

### `migtool klaviyo dedupe check`

Read-only. Checks that each row of a dedupe file landed as its role intends. For every row:

- the profile exists (looked up by the row's email, or its phone for phone-only rows);
- **every field and property the role sends** matches what's stored, with its type;
- the migration tags (where the role adds them);
- list membership, compared by profile ID;
- consent: `Subscribe` rows are subscribed and **not suppressed**. For `new` (profiles the migration creates) the subscription date must match the file's. `kept` profiles are existing subscribers, and Klaviyo keeps their existing date, so it isn't checked. `Unsubscribed` rows are unsubscribed;
- rows carrying a CA suppression (02, 03d) are suppressed.

Rows that don't match go to `<run>.mismatches.csv` with the reasons, and the command exits non-zero. It takes **the same role and options as the import**, and requires them.

| Flag | |
|---|---|
| `--instance` (required) | Instance to check |
| `--file` (required) | The dedupe CSV that was imported |
| `--role` (required) | The role it was imported with |
| `--join-list` | The list the profiles should be on |
| `--subscribe-list` | The list `Subscribe` rows should be on (required when the file has `Subscribe` rows) |
| `--types-from` | The CA `profiles export` CSV giving property types (required for `new`) |
| `--us-snapshot` | The pre-migration US `profiles export` CSV (required for `suppress`) |
| `--limit` | Only the first N rows (e.g. after a pilot) |

```
uv run migtool klaviyo dedupe check --instance klaviyo_us --role new --file dedupe/exports/03a_new_subscribed.csv --join-list T7TTAp --subscribe-list XrGL9u --types-from exports/mainrun/klaviyo_ca/profiles/20260925T151451Z.csv
uv run migtool klaviyo dedupe check --instance klaviyo_us --role suppress --file dedupe/exports/02_suppressions.csv --join-list Sc9zHg --us-snapshot exports/mainrun/klaviyo_us/profiles/20260925T153516Z.csv
uv run migtool klaviyo dedupe check --instance klaviyo_us --role hold --file dedupe/exports/05_release_hold.csv --limit 100
```

### `migtool klaviyo suppressions export`

Writes one row per email suppression: `email`, `profile_id`, `reason`, `timestamp`.

| Flag | |
|---|---|
| `--instance` (required) | Instance to export from |
| `--since` | Only suppressions after this time |
| `--resume` | Continue the unfinished export with the same options |

```
uv run migtool klaviyo suppressions export --instance klaviyo_ca
uv run migtool klaviyo suppressions export --instance klaviyo_ca --since 2026-10-01T00:00:00Z
uv run migtool klaviyo suppressions export --instance klaviyo_ca --resume
```

### `migtool klaviyo suppressions import`

Suppresses every email in the file (a `suppressions export` file, or any CSV with an `email` column). An email with no profile first gets one, tagged `migrated_from=ca` and `migration_run_id`. Then all the emails are submitted for suppression. Klaviyo applies suppression jobs in the background, which took two to four hours in the sandbox, so confirm the result later with `suppressions check`.

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--file` (required) | CSV with an `email` column |
| `--limit` | Only the first N rows. Use `--limit 1` for the one-address pilot. |
| `--as-unsubscribe` | Unsubscribe instead of suppressing. It blocks marketing email immediately, but a later subscribe lifts it, and Klaviyo shows the reason as "Unsubscribed". Use it as a floor, then suppress in the Klaviyo UI. |
| `--yes` | Skip the typed confirmation |
| `--allow-write-to-source` | Allow writing to a `_ca` instance |
| `--retries-settled` | Continue after earlier writes were retried following a lost response (see [How writes are protected](#how-writes-are-protected)) |

```
uv run migtool klaviyo suppressions import --to klaviyo_us --file ca_suppressions.csv --limit 1
uv run migtool klaviyo suppressions import --to klaviyo_us --file ca_suppressions.csv
uv run migtool klaviyo suppressions import --to klaviyo_us --file ca_suppressions.csv --as-unsubscribe
uv run migtool klaviyo suppressions import --to klaviyo_sandbox --file trial_suppressions.csv --yes --retries-settled
uv run migtool klaviyo suppressions import --to klaviyo_ca --file ca_fix.csv --allow-write-to-source   # never needed for the migration
```

### `migtool klaviyo suppressions check`

Read-only. Reports each email's current state in the instance: `suppressed`, `unsubscribed` (only unsubscribed), `not suppressed` or `no profile`. Writes the ones that aren't suppressed to `<run>.not_suppressed.csv`, which can be fed back into `suppressions import`.

| Flag | |
|---|---|
| `--instance` (required) | Instance to check |
| `--file` (required) | CSV with an `email` column |

```
uv run migtool klaviyo suppressions check --instance klaviyo_us --file ca_suppressions.csv
```

### `migtool klaviyo lists export`

Writes `<run>.lists.csv` (ID, name, dates, opt-in setting, member count) and `<run>.list_members.csv` (list ID and name, profile ID, email, join date).

| Flag | |
|---|---|
| `--instance` (required) | Instance to export from |
| `--since` | Only memberships that joined after this time |

```
uv run migtool klaviyo lists export --instance klaviyo_ca
uv run migtool klaviyo lists export --instance klaviyo_ca --since 2026-10-01T00:00:00Z
```

### `migtool klaviyo lists add`

Adds every profile in the file to one list, by the `email` column, or by `phone_number` for a row with no email (a phone-only profile; the number must be in `+international` form). The file can be just emails and phone numbers (for example `list_members.csv` filtered by hand) or the full profile layout.

- Existing profiles are only added to the list. Their fields and consent don't change.
- Missing profiles are created from the file's fields, following the same rules as `profiles import`, and tagged `migrated_from=ca` and `migration_run_id`.
- If the file has `consent` and `consent_timestamp` columns, `SUBSCRIBED` rows (not suppressed) are also subscribed to the list, with their original timestamp. Without those columns, nobody's consent changes.

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--list` (required) | ID of the list to add profiles to |
| `--file` (required) | CSV with an `email` column, and optionally `phone_number` for phone-only rows |
| `--existing-only` | Only add profiles that already exist; skip (and list) the rest |
| `--source` | The `migrated_from` tag for profiles it creates (default `ca`) |
| `--limit` | Only the first N rows |
| `--yes` | Skip the typed confirmation |
| `--allow-write-to-source` | Allow writing to a `_ca` instance |
| `--retries-settled` | Continue after earlier writes were retried following a lost response (see [How writes are protected](#how-writes-are-protected)) |

```
uv run migtool klaviyo lists add --to klaviyo_us --list AbC123 --file vip_members.csv
uv run migtool klaviyo lists add --to klaviyo_sandbox --list Td8hfk --file vip_members.csv --limit 5 --yes --retries-settled
uv run migtool klaviyo lists add --to klaviyo_ca --list XyZ789 --file members.csv --allow-write-to-source   # never needed for the migration
```

### `migtool klaviyo lists copy`

Copies one list's members into a list on another instance, in one step. It reads the source list (read-only), writes its members' emails (and phone numbers, for phone-only members) to `exports/<from>/lists-copy/<run>.members.csv`, then adds them exactly as `lists add` does, to an existing list (`--to-list`) or to one it creates (`--create`, named like the source list plus ` (CA)`, unless `--suffix` or `--name` is given).

- Members are matched by email, or by phone number when they have no email, and only that identifier is sent, so existing profiles get no field, property or consent change. Nobody is subscribed (by email or SMS).
- Members with neither an email nor a phone number are skipped and counted.
- A member with no profile at the destination is created, tagged `migrated_from` with the source (`ca` for `klaviyo_ca`, otherwise the instance name, such as `sandbox`) and `migration_run_id`.
- If a copy is interrupted, don't just re-run it: that reads the source again. Finish it from the saved snapshot with `lists add --to <instance> --list <new list ID> --file <the .members.csv>`; the run prints this command when it fails, with the same `--limit`, `--existing-only` and `--source`.
- With `--existing-only`, a member with no profile at the destination is skipped instead of created, and listed in the run's `.skipped.csv`. Use it when new source profiles should come over through the import (with their consent) rather than as bare profiles; copy again with `--to-list` afterwards to add them.
- `--create` refuses a name the destination already uses, and creates the list only after you confirm.
- Adding people to a list starts any flow triggered by "Added to List" for it. Check before copying into an existing list.

| Flag | |
|---|---|
| `--from` (required) | Instance to read the list from |
| `--list` (required) | ID of the list to copy |
| `--to` (required) | Instance to write to |
| `--to-list` | ID of an existing destination list |
| `--create` | Create the destination list instead (give exactly one of `--to-list` and `--create`) |
| `--name` | Name for the list `--create` makes, used as is |
| `--existing-only` | Skip members with no profile at the destination instead of creating them |
| `--suffix` | Added to the source list's name when there's no `--name` (default ` (CA)`) |
| `--limit`, `--yes`, `--allow-write-to-source`, `--retries-settled` | As for `lists add` |

```
uv run migtool klaviyo lists copy --from klaviyo_ca --list AbC123 --to klaviyo_us --create
uv run migtool klaviyo lists copy --from klaviyo_ca --list AbC123 --to klaviyo_us --create --name "VIP Canada"
uv run migtool klaviyo lists copy --from klaviyo_ca --list AbC123 --to klaviyo_us --to-list XyZ789
```

Klaviyo segments can't have members added directly. To mirror a segment, add its members to a list and build the segment on membership of that list, or copy it as a static list with `segments copy`.

### `migtool klaviyo segments copy`

Copies a segment's **current** members into a static list on another instance, for segments whose rules won't work at the destination. It works exactly like `lists copy`, but reads `--segment` instead of `--list`, and a list it creates is named after the segment plus ` (CA segment)` by default.

The list is a snapshot: it doesn't gain or lose members as the source segment changes. Copy close to when the list is needed, or copy again into the same list with `--to-list` to add later members (nobody is removed). To finish an interrupted copy, use the saved snapshot as for `lists copy`, rather than re-running (a new snapshot would miss anyone who has left the segment since).

| Flag | |
|---|---|
| `--from` (required) | Instance to read the segment from |
| `--segment` (required) | ID of the segment to copy |
| `--to` (required) | Instance to write to |
| `--to-list`, `--create`, `--name`, `--existing-only` | As for `lists copy` |
| `--suffix` | Added to the segment's name when there's no `--name` (default ` (CA segment)`) |
| `--limit`, `--yes`, `--allow-write-to-source`, `--retries-settled` | As for `lists add` |

```
uv run migtool klaviyo segments copy --from klaviyo_ca --segment AbC123 --to klaviyo_us --create
uv run migtool klaviyo segments copy --from klaviyo_ca --segment AbC123 --to klaviyo_us --to-list XyZ789
```

### `migtool klaviyo segments export`

Writes `<run>.segments.csv` and `<run>.segment_members.csv` (same shape as `list_members.csv`). `segments.csv` has the ID, name, dates, member count, the event names each segment's rules use, and three yes/no columns:

- `engagement`: Klaviyo email events (opened, clicked, received), and order events (Placed Order, Ordered Product and other Shopify order events).
- `site_activity`: Viewed Product, Active on Site, Added to Cart, Checkout Started, from any integration.
- `third_party`: any other integration (Eventbrite, Loop Returns, …) or custom API events.

Rules on profile properties, consent, location, or list or segment membership, and Klaviyo subscription events, get no label.

| Flag | |
|---|---|
| `--instance` (required) | Instance to export from |

```
uv run migtool klaviyo segments export --instance klaviyo_ca
```

Segments themselves are moved with Klaviyo's **Clone** action in the UI.

### `migtool klaviyo bis export`

Reads "Subscribed to Back in Stock" events and writes:

- `<run>.bis.csv`: STOQ's import template columns, ready to upload. There's one row per email and SKU (the latest signup), so it can go to STOQ as-is (see below).
- `<run>.reference.csv`: the CA variant and product IDs, product and variant names and Klaviyo event ID for each row.
- `<run>.excluded.csv`: every signup that was dropped, with the reason.

| Flag | |
|---|---|
| `--instance` (required) | Instance to export from (`klaviyo_ca`) |
| `--since` | Only signups after this date or time, e.g. `2026-09-01` |

```
uv run migtool klaviyo bis export --instance klaviyo_ca
uv run migtool klaviyo bis export --instance klaviyo_ca --since 2026-10-01
```

### `migtool klaviyo events resend`

Re-sends orders' Shopify **Placed Order** data as a custom event, to trigger a flow for orders whose original flow email was blocked (for example by `migration_hold`). Each event is an exact copy of the order's Placed Order properties, `$extra` included, so a copy of the Order Confirmation email renders as it would have. It is sent to the customer's profile, or with `--send-to` to a test address.

- The file needs `email` (the customer) and `order_id` or `order_name`. When both are given, they must name the **same** order; a row that matches no order, or more than one, is reported and not sent. The plan shows the order each row matched (name, ID, date) before you confirm.
- `<run>.results.csv` records each order's source event, recipient, `unique_id` and outcome (`submitted`, `refused: …`, `not sent: …`, `not attempted`), also when the run is interrupted. "Submitted" means Klaviyo accepted the event; check the flow's emails for delivery.
- Klaviyo creates the metric the first time it receives the event, and a flow can only use it as a trigger after that. So send a test to yourself first, then build the flow on the new metric.
- Each event has a fixed `unique_id` per order and recipient, so running it again doesn't send twice.
- It needs the `events:write` scope.

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--file` (required) | CSV with `email` and `order_id` or `order_name` |
| `--metric` (required) | Name of the custom metric (the resend flow's trigger) |
| `--send-to` | Send every event to this email instead (tests) |
| `--limit`, `--yes`, `--allow-write-to-source`, `--retries-settled` | As for `lists add` |

```
uv run migtool klaviyo events resend --to klaviyo_us --file held_orders.csv --metric "Order Confirmation – Resend" --send-to me@example.com
uv run migtool klaviyo events resend --to klaviyo_us --file held_orders.csv --metric "Order Confirmation – Resend"
```

## Shopify

The Shopify commands only read, except `shopify consent-sync`, which writes email marketing consent and nothing else. The client refuses to send a GraphQL mutation from any query, even if the token would allow one. Exactly two mutations exist, each sent from one place: the bulk-export start (`customers-export`) and `customerEmailMarketingConsentUpdate` (`consent-sync`). Every command first checks that the token belongs to the store in `SHOPIFY_<US|CA>_SHOP`, so a CA token under the US name is stopped.

### `migtool shopify whoami`

Shows the store a token belongs to and the scopes it was granted, and notes any write scopes.

```
uv run migtool shopify whoami --instance shopify_us
```

### `migtool shopify customers-export`

Exports every customer with their marketing consent to `exports/<instance>/customers-export/<run>.csv`, using a Shopify **bulk export**: Shopify builds the file, typically in minutes. Starting a bulk export takes the `bulkOperationRunQuery` call, the one GraphQL mutation this tool sends. It only reads data, and the query it runs must itself contain no mutation.

- **Recoverable:** the export's Shopify ID is saved in `state/<instance>/` as soon as it starts, so an interrupted run continues the same export. A start whose response is lost isn't retried; the new export is looked up instead.
- **Reuse:** a matching export from the last 24 hours (running or finished) is reused rather than started again; `--fresh` starts a new one.
- **Complete or nothing:** the CSV is written as `.csv.part` and renamed only once every customer Shopify counted has been downloaded.
- The tool runs one export at a time by choice (Shopify allows several).

```
uv run migtool shopify customers-export --instance shopify_us
```

### `migtool shopify customers`

Looks customers up by email and writes `exports/<instance>/customers/<run>.csv`: Shopify's email and SMS marketing state, opt-in level, when consent last changed, tags, order count and country. With `--compare <klaviyo instance>`, each row also has the Klaviyo profile's consent, suppression, `Accepts Marketing` and `migration_hold`, and a `match` column: `same`, `same (Klaviyo suppressed)`, `Shopify yes / Klaviyo no`, `Shopify no / Klaviyo yes`, `no Shopify customer` or `no Klaviyo profile`. The summary counts each.

| Flag | |
|---|---|
| `--instance` (required) | Shopify instance to read |
| `--email` | An email to look up (repeatable) |
| `--file` | A CSV with an `email` column (for example a dedupe file) |
| `--compare` | Klaviyo instance to compare with |

```
uv run migtool shopify customers --instance shopify_us --file pilot.csv --compare klaviyo_us
uv run migtool shopify customers --instance shopify_us --email a@example.com --email b@example.com
```

### Consent sync: Klaviyo US → Shopify US email consent

The plan, decisions (D1–D15), pilot results and run order are in `docs/CONSENT_SYNC.md`; the build spec is phase 6 of `docs/BUILD_PLAN.md`. In short: Shopify's email marketing consent is set to Klaviyo's for every customer whose email matches a Klaviyo profile. Klaviyo subscribed → `SUBSCRIBED`; unsubscribed or suppressed → `UNSUBSCRIBED`; with the original consent date. Shopify can't be set to `NOT_SUBSCRIBED`, so Klaviyo never subscribed + Shopify unsubscribed is left (D12). Klaviyo never subscribed + Shopify subscribed keeps Shopify and subscribes Klaviyo with Shopify's date (D14). SMS isn't touched.

#### `migtool shopify consent-plan`

Local and read-only: joins a Klaviyo `profiles export` and a Shopify `customers-export` by email and writes, under `exports/<instance>/consent-plan/`:

- `<run>.plan.csv`: the Shopify writes (target state and date, and whether Shopify's date is newer)
- `<run>.klaviyo_d14.csv`: the D14 Klaviyo subscribes, in the dedupe layout
- `<run>.excluded.csv`: D12 and unwritable (`INVALID`/`REDACTED`) customers

It prints counts by transition. Future dates are set to the plan time, since Shopify refuses them. `--emails <csv>` limits the plan to those emails, for trials.

```
uv run migtool shopify consent-plan --klaviyo exports/klaviyo_us/profiles/<ts>.csv --shopify exports/shopify_us/customers-export/<ts>.csv
```

#### D14: `klaviyo dedupe import --role consent`

```
uv run migtool klaviyo dedupe import --to klaviyo_us --role consent --file <run>.klaviyo_d14.csv --subscribe-list Xz4KGg
uv run migtool klaviyo dedupe check --instance klaviyo_us --role consent --file <run>.klaviyo_d14.csv --subscribe-list Xz4KGg
```

Klaviyo can take 10–20 minutes to apply back-dated subscribes, so run the check after that.

#### `migtool shopify consent-sync`

The only Shopify write.

- **Checks first:** the token belongs to the store and has `write_customers`, then a typed confirmation of the store and the number of customers.
- **For each batch of 50:**
  - reads the customers' current email consent and email
  - **identity conflict** if a customer's email no longer matches the plan: it isn't written, because the decision came from the Klaviyo profile of the planned email
  - **skips customers already in the target state:** Shopify treats even an identical write as a customer update and notifies apps
  - sends `customerEmailMarketingConsentUpdate` for the rest
- **Results** per customer go to `<run-id>.results.csv` (one file per run): written, skipped, refused (with Shopify's message), not found, identity conflict, or unknown.
- **A lost response is never resent.** The customer is read back instead.
- **Exit code:** 1 if anything in the plan is still unresolved (refused, not found, identity conflict or unknown), including earlier runs' failures that a resume didn't retry, or if the run stopped.

| Flag | |
|---|---|
| `--to` (required) | Shopify instance |
| `--plan` (required) | `<run>.plan.csv` |
| `--target` | Only `SUBSCRIBED` or only `UNSUBSCRIBED` rows (for the canary) |
| `--limit` | At most N customers in this run |
| `--resume` | Continue the saved run of this plan: skips customers written or skipped, retries unknown ones, and reports (but doesn't retry) refused, not found and conflicting ones |
| `--retry-failed` | With `--resume`, also retry refused, not-found and conflicting customers |
| `--yes` | Skip the typed confirmation |

The run is saved in `state/<instance>/shopify-consent-sync.checkpoint.json`, bound to the plan's path, content (sha256), row count and store. `--resume` refuses a plan that has changed since, or a different store: a changed plan is a new run. A new run needs that file deleted.

```
uv run migtool shopify consent-sync --to shopify_us --plan <run>.plan.csv --target SUBSCRIBED --limit 100     # canary
uv run migtool shopify consent-sync --to shopify_us --plan <run>.plan.csv --target UNSUBSCRIBED --limit 100 --resume
uv run migtool shopify consent-sync --to shopify_us --plan <run>.plan.csv --resume                            # the rest
```

#### `migtool shopify consent-validate`

Local and read-only. It works on exports taken after the run and checks that every matched customer's Shopify state matches Klaviyo under the rules (D12 counts as a match; `INVALID`/`REDACTED` are reported separately). Both exports must have the expected columns and values; anything else is refused rather than guessed. It also checks:

- with `--results`: every customer the run wrote is still in the Shopify export, under the same email, in the state and with the date written
- with `--d14`: every D14 customer has a Klaviyo profile and a Shopify customer, both subscribed, with Klaviyo's date equal to Shopify's
- with `--klaviyo-before <backup>`: every Klaviyo consent state, date or method change since the backup, and every profile gone (deleted or merged), is listed in `<run>.klaviyo_changes.csv` for review

Mismatches go to `<run>.mismatches.csv` and exit 1.

```
uv run migtool shopify consent-validate --klaviyo <after>.csv --shopify <after>.csv --results <run>.results.csv --d14 <run>.klaviyo_d14.csv --klaviyo-before <backup>.csv
```

## STOQ: preparing and uploading the Back in Stock file

The `bis.csv` columns and how they're filled:

| Column | Filled with |
|---|---|
| `SKU` | The SKU from the Klaviyo event. SKUs are identical in both stores; CA variant IDs are not, and are only in `reference.csv`. |
| `Email` | The profile's email |
| `Phone` | **Blank on purpose** (see [Warnings](#warnings)) |
| `Name` | First and last name |
| `Market` | **Blank: fill by hand.** Must match a market name or ID in the US Shopify admin. |
| `Quantity` | Blank (STOQ defaults it to 1) |
| `GDPR confirmed` | **Blank: fill by hand if needed**, `true` or `false` (blank means false) |
| `Accepts marketing` | `true` if the profile is currently subscribed to email and not suppressed, otherwise `false` |
| `Language` | The profile's locale as a language code (`en-CA` → `en`) |
| `Date` | The signup date, `dd/mm/yyyy` in UTC |

Before uploading:

1. Open `bis.csv` and review it. Keep the header row unchanged, so STOQ maps the columns automatically.
1. Compare its emails with the Klaviyo profile exports and remove rows for people who are unsubscribed or never subscribed. STOQ's Klaviyo integration may subscribe everyone you upload (see `docs/API_NOTES.md`).
2. Fill `Market` (and `GDPR confirmed`, if used). STOQ watches the CA inventory location for CA subscribers and the US location for US and international ones, so the market decides which stock the alert waits for.
3. Save as **CSV** (not Excel).

To upload: in the US store's Shopify admin, go to **STOQ → Back in stock alerts → Settings → Integrations → Import data**, click **Upload CSV**, check the column mapping (and that dates are read day-first), and start the import. STOQ emails a status report when it finishes, and **View imports** shows past imports. Importing sends no alerts: customers are only notified when the product restocks. Re-uploading is safe, because STOQ skips a customer already waiting on the same variant. It rejects test or disposable addresses (`example.com`, `mailinator.com`, …).

Check the result in **Reports → Back in Stock → Current waitlist**.

## Migration run order

The full order, with the reasons, is in `docs/BUILD_PLAN.md`. In short:

1. **Profiles.** Create the LOF Canada Newsletter list in `klaviyo_us`. Export profiles from `klaviyo_ca` and `klaviyo_us` and dedupe them outside the tool (most recent consent wins; phone numbers unique against US; remove obviously overlapping Shopify properties). Then import the profiles unique to CA:
   ```
   uv run migtool klaviyo profiles import --to klaviyo_us --file ca_unique.csv --list-id <LOF Canada Newsletter ID>
   ```
2. **Suppressions.** Export from `klaviyo_ca` and edit the file to choose which to apply. Pilot one address with `--limit 1`, then run `suppressions check` on it until it shows `suppressed` (hours, in the sandbox). Then import the rest and check them the same way. If the pilot never applies, import with `--as-unsubscribe` and suppress in the Klaviyo UI.
3. **Lists and segments.** Export both from `klaviyo_ca`. Copy a whole list with `lists copy`, attach chosen sets of profiles to US lists with `lists add`, or use the Klaviyo UI. Clone segments in the UI.
4. **Back in Stock.** `bis export` from `klaviyo_ca`, prepare the file (above), including removing unsubscribed and never-subscribed people, and upload it in the US store's STOQ admin.
5. **Catch-up run.** Do this immediately before sign-ups are turned off on the CA Klaviyo site (next section).
6. **Delete local data** (below), keeping `exports/og_exports/`.

## Catch-up run

Repeat steps 1–4 for anything that changed since the main run, with `--since` set a little before the main run started. Every write is safe to repeat, so overlap does no harm.

```
uv run migtool klaviyo profiles export     --instance klaviyo_ca --since 2026-10-01T00:00:00Z
uv run migtool klaviyo suppressions export --instance klaviyo_ca --since 2026-10-01T00:00:00Z
uv run migtool klaviyo lists export        --instance klaviyo_ca --since 2026-10-01T00:00:00Z
uv run migtool klaviyo bis export          --instance klaviyo_ca --since 2026-10-01
```

- **The suppressions export is required.** Unsubscribes don't change a profile's update time, so only the suppressions export catches them.
- Dedupe the delta files the same way as the main run, then import them with the same commands.
- STOQ skips signups it already has, so the Back in Stock delta can be uploaded as-is.

## Deleting local data

The exports hold customer personal data. As soon as the migration is finished, delete everything under `exports/` **except `exports/og_exports/`**, and `state/`:

```
find exports -mindepth 1 -maxdepth 1 ! -name og_exports -exec rm -rf {} +
rm -rf state/
```

**`exports/og_exports/`** is a snapshot of the original exports, kept on purpose. It includes the first full `klaviyo_ca` exports of 2026-09-24: profiles, suppressions, segments and Back in Stock (the lists export there covers only memberships since 2026-09-01). It also has the sandbox trial files. It still holds CA customer personal data, is git-ignored, and should be deleted when it's no longer needed. The tool never writes to it. Its profile files predate typed property columns, so re-export rather than re-import from them.

## Troubleshooting

- **`KLAVIYO_…_API_KEY is not set`**: add it to `.env` in the repository root, and run commands from there.
- **`401 Incorrect authentication credentials`**: the key is wrong or revoked. Create a new private key in Klaviyo (Settings → API keys).
- **`403 … missing required scopes`**: the key lacks a scope listed in [Instances and keys](#instances-and-keys).
- **An export stopped part-way**: run it again with `--resume` and the same options. Starting it without `--resume` abandons the unfinished one.
- **Suppressions don't show up**: wait (hours), then run `suppressions check`. Don't go by the Klaviyo job status.
