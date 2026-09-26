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

Keys never appear in output, logs or error messages. Check a key before using it:

```
uv run migtool klaviyo whoami --instance klaviyo_us
```

Run the tests with `uv run pytest`. They use recorded responses and never call a live account.

## How writes are protected

Every write command (`profiles import`, `suppressions import`, `lists add`, `lists copy`):

1. names its target with `--to <instance>`;
2. prints the account name and ID, the instance and the number of records, and asks you to type the instance name to confirm (`--yes` skips this, for scripted runs);
3. refuses to write to a `_ca` instance unless `--allow-write-to-source` is given, even with `--yes`.

Every write can be repeated safely. Re-importing a profile updates it, adding a profile to a list it's already on changes nothing, and suppressing a suppressed email changes nothing. A failed import is simply re-run; imports don't resume.

When a write goes wrong partway:

- **Bad rows:** Klaviyo refuses a whole batch if one row is invalid (a malformed email, say), and says which row. The tool drops that row, resends the rest, and lists refused rows with Klaviyo's reason in the errors file. A profile over Klaviyo's 100 KB per-profile limit is refused before sending.
- **Bad settings:** an error about the request itself rather than a row (for example a `--list-id` that doesn't exist) stops the run straight away, recorded as `aborted`. Fix the option and re-run.
- **Unknown outcomes:** counts come from Klaviyo's own job results. A profile whose result can't be confirmed (job still running, failed, or its error list unreadable) is counted as failed with "outcome unknown", never as written.
- **Retried writes:** if a write fails in a way that means Klaviyo may already have received it (a lost response, a server error), it's retried with a warning and **recorded in `state/<instance>/ambiguous_writes.json`**. Repeating the same write is safe, but a delayed first copy could land after a *later, different* write (say, re-setting a hold after 05 released it). So **the next write command stops** until you've waited a few minutes and confirmed the earlier file with `dedupe check`; then re-run it with `--retries-settled`, which clears the record.
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

### `migtool klaviyo dedupe import`

Imports one of the dedupe files (Klaviyo UI-import layout) under the rules of its role. **The file-by-file rules, list IDs and full command sequence are in `docs/DEDUPE_IMPORT.md`.**

| Role | Files | Behaviour |
|---|---|---|
| `hold` | 01, 05 | Updates existing profiles only. Sends `migration_hold` and nothing else. No list, no consent, no tags. |
| `hold-new` | 01b | Creates or updates. `migration_hold` plus migration tags. No list, no consent. |
| `suppress` | 02 | Rows that were US profiles before the migration (per `--us-snapshot`) join `--join-list` and get `ca_suppression_*`. The rest are CA-only: created or updated and tagged. Then all are submitted for suppression. |
| `new` | 03a–03d | Creates or updates, joins `--join-list`, adds tags. Consent from `Email Marketing Consent`: `Subscribe` → historical subscribe to `--subscribe-list`; `Unsubscribed` → unsubscribe. |
| `kept` | 04a, 04b | Updates existing profiles only, joins `--join-list`, adds tags. Consent as for `new`, using `ca_consent_timestamp`. |

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--file` (required) | The dedupe CSV |
| `--role` (required) | `hold`, `hold-new`, `suppress`, `new` or `kept` |
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

Adds every profile in the file to one list, by the `email` column. The file can be just emails (for example `list_members.csv` filtered by hand) or the full profile layout.

- Existing profiles are only added to the list. Their fields and consent don't change.
- Missing profiles are created from the file's fields, following the same rules as `profiles import`, and tagged `migrated_from=ca` and `migration_run_id`.
- If the file has `consent` and `consent_timestamp` columns, `SUBSCRIBED` rows (not suppressed) are also subscribed to the list, with their original timestamp. Without those columns, nobody's consent changes.

| Flag | |
|---|---|
| `--to` (required) | Instance to write to |
| `--list` (required) | ID of the list to add profiles to |
| `--file` (required) | CSV with an `email` column |
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

Copies one list's members into a list on another instance, in one step. It reads the source list (read-only), writes its members' emails to `exports/<from>/lists-copy/<run>.members.csv`, then adds them exactly as `lists add` does, to an existing list (`--to-list`) or to one it creates (`--create`, named like the source list unless `--name` is given).

- Members are added by email only, so existing profiles get no field, property or consent change. Nobody is subscribed.
- Members with no email (phone-only) are skipped and counted.
- An email with no profile at the destination is created, with the migration tags.
- `--create` refuses a name the destination already uses, and creates the list only after you confirm.
- Adding people to a list starts any flow triggered by "Added to List" for it. Check before copying into an existing list.

| Flag | |
|---|---|
| `--from` (required) | Instance to read the list from |
| `--list` (required) | ID of the list to copy |
| `--to` (required) | Instance to write to |
| `--to-list` | ID of an existing destination list |
| `--create` | Create the destination list instead (give exactly one of `--to-list` and `--create`) |
| `--name` | Name for the list `--create` makes |
| `--limit`, `--yes`, `--allow-write-to-source`, `--retries-settled` | As for `lists add` |

```
uv run migtool klaviyo lists copy --from klaviyo_ca --list AbC123 --to klaviyo_us --create
uv run migtool klaviyo lists copy --from klaviyo_ca --list AbC123 --to klaviyo_us --create --name "VIP (from CA)"
uv run migtool klaviyo lists copy --from klaviyo_ca --list AbC123 --to klaviyo_us --to-list XyZ789
```

Klaviyo segments can't have members added directly. To mirror a segment, add its members to a list and build the segment on membership of that list.

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
