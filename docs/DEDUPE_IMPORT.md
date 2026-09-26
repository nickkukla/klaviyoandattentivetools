# Importing the dedupe files

The CA and US profile exports were deduplicated outside the tool (in DuckDB), under consent rules the client signed off on. That produced the files below, in Klaviyo's UI-import layout. `migtool klaviyo dedupe import` imports each one under the rules of its **role**.

## Resolution rules (summary)

- Suppression wins. A hard bounce, spam complaint, manual suppression or invalid email on either side keeps the profile suppressed. An unsubscribe isn't a suppression.
- Otherwise the most recent consent change wins (`consent_last_updated`, UTC). A subscribe or unsubscribe counts. Ties go to US.
- A side with a consent event beats a never-subscribed side. If both never subscribed, US wins.
- **CA wins:** the US profile gets CA's consent, `market=CA`, the migration tags and the audit fields. Its names, address and other properties are left alone.
- **US wins:** the US profile is left alone except for `migration_hold`.

`migration_hold` (a boolean) is set on every profile linked to a CA customer, so flows can be held back while CA Shopify orders are migrated. File 05 clears it afterwards.

## Files, roles and lists

| File | Role | Creates? | Joins list | Consent | Migration tags |
|---|---|---|---|---|---|
| `01_hold_only` | `hold` | no (existing only) | none | none | no |
| `01b_hold_shopify_only` | `hold-new` | yes | none | none | yes |
| `02_suppressions` | `suppress` | yes (the CA-only ones) | existing ones → **Updated US Profiles by CA Migration** | suppression | new ones only |
| `03a`–`03d` | `new` | yes | **Migrated CA profiles** | 03a: subscribe to **LOF Canada Newsletter** (historical, original timestamp). 03b: unsubscribe. 03c, 03d: none. | yes |
| `04a`, `04b` | `kept` | no (existing only) | **Updated US Profiles by CA Migration** | 04a: subscribe to **LOF Canada Newsletter** (historical, `ca_consent_timestamp`). 04b: unsubscribe. | yes |
| `05_release_hold` | `hold` | no (existing only) | none | none | no |

List IDs in `klaviyo_us`: Migrated CA profiles `T7TTAp`, Updated US Profiles by CA Migration `Sc9zHg`, LOF Canada Newsletter `XrGL9u`. All three are single opt-in.

Nobody is bulk-added to the LOF Canada Newsletter: people only reach it through the historical subscribe, which Klaviyo documents as skipping "Added to list" flows.

## How the columns are read

| Column | Becomes |
|---|---|
| `email`, `phone_number` | The identifier: email, or phone (E.164, with `+`) when there's no email |
| `first_name`, `last_name`, `organization`, `title`, `locale` | Klaviyo's standard fields |
| `location_<field>` | Klaviyo's location fields |
| `Email Marketing Consent` | **The consent instruction:** `Subscribe` → subscribe, `Unsubscribed` → unsubscribe, blank → no change. Only roles `new` and `kept` apply it. |
| `Email Marketing Consent Timestamp`, else `ca_consent_timestamp` | The historical consent date for a subscribe |
| `ca_consent` | Audit property only. **Never** an instruction (03d's suppressed rows carry `SUBSCRIBED` here). |
| `migration_hold` | Boolean property (`true`/`false`, any case) |
| `migration_source` | Not sent; the tool writes `migrated_from=ca` instead |
| `ca_consent_method_detail` | Written as `ca_consent_source` |
| any other column | A custom property under its own name. Its type comes from the CA `profiles export` header (`--types-from`), so `Shopify Tags` is sent as a list and `Checked in` as true/false. |

Migration tags are `migrated_from=ca` and `migration_run_id` (one per run, in the manifest).

## Order and commands

Before starting, **confirm that every destination flow excludes `migration_hold = true`** (all flows are gated on profile triggers for CA members). Give the `klaviyo_us` key write scopes.

```
T=exports/mainrun/klaviyo_ca/profiles/20260925T151451Z.csv
D=dedupe/exports
uv run migtool klaviyo dedupe import --to klaviyo_us --role hold      --file $D/01_hold_only.csv
uv run migtool klaviyo dedupe import --to klaviyo_us --role hold-new  --file $D/01b_hold_shopify_only.csv
uv run migtool klaviyo dedupe import --to klaviyo_us --role suppress  --file $D/02_suppressions.csv --join-list Sc9zHg
uv run migtool klaviyo dedupe import --to klaviyo_us --role new       --file $D/03a_new_subscribed.csv --join-list T7TTAp --subscribe-list XrGL9u --types-from $T
uv run migtool klaviyo dedupe import --to klaviyo_us --role new       --file $D/03b_new_unsubscribed.csv --join-list T7TTAp --types-from $T
uv run migtool klaviyo dedupe import --to klaviyo_us --role new       --file $D/03c_new_never_subscribed.csv --join-list T7TTAp --types-from $T
uv run migtool klaviyo dedupe import --to klaviyo_us --role new       --file $D/03d_new_suppressed.csv --join-list T7TTAp --types-from $T
uv run migtool klaviyo dedupe import --to klaviyo_us --role kept      --file $D/04a_kept_subscribed.csv --join-list Sc9zHg --subscribe-list XrGL9u
uv run migtool klaviyo dedupe import --to klaviyo_us --role kept      --file $D/04b_kept_unsubscribed.csv --join-list Sc9zHg
# … Shopify customer and order migration …
uv run migtool klaviyo dedupe import --to klaviyo_us --role hold      --file $D/05_release_hold.csv
```

Pilot first: run 03a and 03b with `--limit 5` and inspect those profiles in Klaviyo before running the rest.

**Check every import.** After each file, run `klaviyo dedupe check` with the same role and lists. It reads each row's profile back and confirms it landed as intended: `migration_hold`, tags, list membership, and consent or suppression. For 02, give suppressions time to apply (minutes to hours) before checking. For example:

```
uv run migtool klaviyo dedupe check --instance klaviyo_us --role new  --file $D/03a_new_subscribed.csv --join-list T7TTAp --subscribe-list XrGL9u
uv run migtool klaviyo dedupe check --instance klaviyo_us --role kept --file $D/04b_kept_unsubscribed.csv --join-list Sc9zHg
```

A clean check ends `mismatched 0`. Otherwise `<run>.mismatches.csv` lists each row that's off and why. An import's own summary counts what Klaviyo *accepted*. The check confirms what actually *landed*.

Each run first works out a plan and shows it before asking for confirmation: rows to send, how many already exist and how many are new, rows skipped or unreadable, the lists by name, and the subscribe and unsubscribe counts. Update-only roles (`hold`, `kept`) skip any row with no existing profile, rather than create a stub, and list it in `<run>.skipped.csv`. For 05, run it last: the profiles 03 and 01b create only exist once those imports are done.

Suppressions from 02 apply in the background, in two to four hours in the sandbox. Confirm them with `klaviyo suppressions check`.

## If an import is interrupted

**Re-run the same file with the same command.** There's no separate resume: every role is safe to repeat, and the dev-account pilot re-ran all ten files with 0 failures and the same end state.

- `hold`, `hold-new`: setting the same properties again changes nothing.
- `suppress`: suppressing a suppressed email changes nothing. Profiles a first run created are recognised by their `migrated_from=ca` tag, so they're handled as CA profiles again and never added to the Updated US Profiles list.
- `new`, `kept`: re-importing the same fields is a no-op. A repeated historical subscribe on someone already subscribed is either accepted (same or earlier date) or, if dated later, handled as "already subscribed" (list-only add). Repeating an unsubscribe leaves them unsubscribed.

Each run gets a new `migration_run_id`, so a profile shows the most recent run that touched it. Then run `dedupe check` to confirm the whole file landed.

## The catch-up run

Repeat the DuckDB classification on the `--since` delta exports, regenerate the same set of files (smaller), and import them with the same commands. Every write is safe to repeat.

## Trial (2026-09-25, `klaviyo_sandbox` only)

Files with the real files' exact headers, filled with `test16`–`test25@0xb8.net` and two made-up `+1 416 555 01xx` numbers, were imported in the order above into three trial lists. Every run had 0 failures, and every profile came out as designed:

- **hold (01, by email and by phone):** only `migration_hold=true` (boolean) was added. No list, no consent change, no tags, and the existing name and properties were untouched.
- **hold-new (01b):** created never subscribed, with the hold and tags, and no list.
- **suppress (02):** the existing profile joined Updated and was suppressed, with `ca_suppression_reason` and no tags. The new one was created, tagged and suppressed. Suppressions applied within about 3 minutes (hours in earlier trials).
- **new (03a):** joined Migrated and Newsletter, subscribed with the **original** timestamps (2022-05-05, 2023-06-06). `Shopify Tags` arrived as a list, `Checked in` as true, `Current Balance` as 12.5, and `coupon` stayed the text `"123"`. `ca_consent_source` was renamed, and `migration_source` wasn't sent.
- **new (03b):** joined Migrated and was unsubscribed. **03c** (including a phone-only row): joined Migrated, no consent change.
- **new (03d):** joined Migrated and stayed suppressed, **not subscribed**, despite `ca_consent=SUBSCRIBED`.
- **kept (04a):** joined Updated and Newsletter, subscribed with `ca_consent_timestamp` (2024-08-09), with tags. The US name and properties were kept.
- **kept (04b):** a subscribed US profile joined Updated and was unsubscribed, with tags.
- **hold (05):** `migration_hold` became `false` on all 12 profiles.
- No `Received Email` events on any trial profile.

## Real-data pilot (2026-09-25, `klaviyo_sandbox` only)

The first 10 rows of each real file were imported into three pilot lists in the dev account. Many of these customers already exist there, so it exercised updates as well as creates. **Klaviyo accepted every real row:** nothing was unreadable, and no import refused a profile. It found two things, both now fixed:

1. **Already-subscribed profiles.** Klaviyo refuses a historical subscribe dated **after** a profile's existing, earlier subscription ("backdated consent date … is after current subscription date"). That hit 1 of 10 in 03a and 5 of 10 in 04a. On the real run it would affect most of 04a: about 16,466 of 16,935 are already subscribed in US, and CA won because its date is later. The tool now treats that refusal as **already subscribed**: it adds the profile to `--subscribe-list` with a list-only import, keeping its existing subscription date and consent, and counts it under `steps.subscribe_list_only` rather than as an error. On the re-run, all 20 of 03a's and 04a's pilot rows were on the pilot newsletter list, with 0 failures. Any other subscribe refusal is still an error.
2. **Re-running 02.** On a second run, profiles 02 had created were "existing", so they were handled as US profiles and joined Updated US Profiles. Now an existing profile counts as a US profile only if it isn't tagged `migrated_from=ca`. A re-run of the pilot showed "6 existing, 4 new", the same as the first run.

## Keep these files out of Excel

Opening and saving these CSVs in Excel damages them: phone numbers lose their `+`, long IDs turn into scientific notation, and dates, zip codes and some addresses are rewritten. Regenerate from DuckDB, or edit with a script. To look at a file in Excel, use Data → From Text/CSV with every column set to Text, and don't save it.
