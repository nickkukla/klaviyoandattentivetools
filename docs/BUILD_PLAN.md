# Build plan

This plan builds what `docs/REQUIREMENTS.md` specifies. Phases are ordered by dependency and risk: shared foundations first, then read-only checks against the real APIs, then exports (safe), then writes (risky).

Each phase ends with a **gate**: the checks that must pass before the next phase starts. Gates use the acceptance criteria in the spec.

## Layout

```
pyproject.toml            # uv project; console script `migtool`
.env.example              # every variable, no values
src/migtool/
  cli.py                  # typer app; sub-app: klaviyo
  config.py               # instance registry + .env loading
  http.py                 # shared client: retries, Retry-After, rate limiting
  safety.py               # --to / confirm prompt / --yes / --allow-write-to-source
  output.py               # exports/<instance>/<object>/<ts>.*, manifest.json
  runlog.py               # errors.csv, run summary, exit code
  state.py                # resume checkpoints, saved job IDs (state/)
  klaviyo/  client.py profiles.py suppressions.py lists.py segments.py bis.py
tests/
  fixtures/               # recorded API responses, secrets scrubbed
docs/
  REQUIREMENTS.md
  BUILD_PLAN.md
  API_NOTES.md            # findings from phase 1
README.md
```

`exports/`, `state/` and `.env` are git-ignored.

## Phase 0: Foundations

Build the shared parts every command uses.

- `uv` project, `typer` CLI skeleton with a `klaviyo` sub-command, `pytest` + `respx` for recorded-response tests.
- **Config:** instances `klaviyo_ca`, `klaviyo_us`, `klaviyo_sandbox`, each mapped to its `.env` variable. A missing variable is a clear error naming the variable. Keys never appear in logs or errors.
- **HTTP client:** retries 429 and 5xx up to 6 times with increasing waits, honouring `Retry-After`. Per-endpoint rate limiter (Klaviyo publishes burst and steady limits per endpoint).
- **Safety:** `--to` required on writes; prompt shows account, instance and record count; typed confirmation; `--yes`; `--allow-write-to-source` for `_ca` targets.
- **Output:** CSV writer (JSONL fallback for exports too complex for CSV), UTC ISO 8601 timestamps, `manifest.json` with counts per run.
- **Run log:** `<run>.errors.csv`, end-of-run summary (read, written, skipped, failed), non-zero exit on any failure.
- **State:** checkpoint files for export `--resume`; saved bulk job IDs.

**Gate:** unit tests pass for retries (fake 429 with `Retry-After`), rate limiter pacing, the confirmation prompt (including a refused `_ca` write), and resume from a checkpoint.

## Phase 1: API checks (read-only, safe in production)

Confirm the facts the design depends on before building features. Record results in `docs/API_NOTES.md`, and update the spec if anything contradicts it.

| Check | Why it matters |
|---|---|
| `whoami` against every configured instance; `klaviyo_sandbox` must be account `T2aEdf` | Keys work and point at the expected accounts |
| ~~Attentive: does List Segments include segments built in the UI, or only API-created ones?~~ | Answered: API-created only, so Attentive was dropped from the tool |
| Klaviyo: which API revision returns segment rule definitions, and their shape | Segment labels (engagement / site activity / third-party) depend on it |
| Klaviyo: map metric IDs to their source integration | Same |
| Klaviyo: name of the Back in Stock metric in `klaviyo_ca`, and the event property keys holding variant and product IDs | Back in Stock export |
| Klaviyo: catalog variants expose SKU and inventory quantity | SKU lookup and in-stock filtering |
| Klaviyo: batch limits and request shape for bulk profile import, historical-import subscribe, and bulk suppression | Import design |
| Klaviyo: which suppression reasons and dates the profile data reports, and whether they can be filtered by date | Suppressions export columns and `--since` |
| Klaviyo: does a consent or suppression change update a profile's `updated` time, and can profiles, list memberships and Back in Stock events be filtered by time? | Catch-up run relies on `--since` catching every change |
| Klaviyo: bulk import with a list relationship adds profiles to a list without changing consent, and creates profiles for emails that have none | `lists add` |
| Klaviyo: which Back in Stock event or profile fields map to STOQ's `Name`, `Language`, `Accepts marketing` and `Quantity` | STOQ output columns |
| Klaviyo: historical-import subscribe skips double opt-in and doesn't trigger list flows | No emails sent on import |
| ~~STOQ: v1 intents on the dev store~~ | Answered: the API needs US Shopify IDs, not SKUs, so `stoq import` was dropped |

Record real responses (secrets and personal data scrubbed) as test fixtures while doing this.

**Gate:** every check answered in `API_NOTES.md`. Any change to the spec agreed with the user.

## Phase 2: Klaviyo exports (read-only)

- `klaviyo profiles export` with the full CSV layout from the spec, `--resume`, `--with-predictive`, `--segment`, `--since`.
- `klaviyo lists export` → `lists.csv`, `list_members.csv`, with `--since`.
- `klaviyo segments export` → `segments.csv` with the three label columns and event names, `segment_members.csv`.
- `klaviyo suppressions export`: every suppression with email, reason and date; `--since`.

**Gate:**
- Full profile export of `klaviyo_ca` completes (about 291k rows).
- An export interrupted mid-run finishes correctly with `--resume`, with no duplicate or missing rows.
- Segment labels spot-checked against five segments with known rules.
- `--since` on each export returns only records changed after the timestamp.

## Phase 3: Klaviyo writes

- `klaviyo profiles import`: bulk profile import, then historical-import subscribe (to `--list-id`) using the original consent timestamp, then unsubscribe/suppress where the CSV says so. Writes `ca_consent_method`, `ca_consent_source`, `ca_suppression_reason`, `ca_suppression_timestamp`, `ca_external_id` (from the `external_id` column; the source profile `id` is never sent), `migrated_from=ca` and `migration_run_id` (generated per run, recorded in `manifest.json`). Imports `phone_number`. Supports `--limit`.
- `klaviyo whoami`.
- `klaviyo suppressions import` (suppression jobs took two to four hours to apply in the sandbox, and their status is misleading, so the import doesn't wait on them; `klaviyo suppressions check` confirms the result; `--as-unsubscribe` and the Klaviyo UI are the fallbacks): suppresses every email in the file, whatever its consent in the destination; creates missing profiles as suppressed.
- `klaviyo lists add`: adds every profile in the file to one list; creates missing profiles; writes consent only when the file has consent columns; safe to repeat.
- All three track Klaviyo's background jobs and put per-record errors in the errors file.

**Trial** in `klaviyo_sandbox` (`T2aEdf`):
- 10 profiles on addresses we control, mixing subscribed, unsubscribed, suppressed and never-subscribed.
- 5 suppressions, including one profile that is subscribed in the sandbox.
- 5 profiles added to a list (email only, including one with no existing profile), then the same file added again; and 5 more from a file with consent columns.
- A catch-up pass: change some trial profiles, export with `--since`, re-import.

**Gate:** re-exporting the trial profiles shows the same consent status and timestamp, the `ca_*`, `migrated_from` and `migration_run_id` properties are set, the destination `external_id` is untouched, suppressed profiles are still suppressed, never-subscribed profiles are unchanged, no welcome or double opt-in emails were sent, the 5 suppressions appear in the suppression list (the subscribed one is now suppressed), the email-only list add changed no consent, created the missing profile and its second run changed nothing, the list add with consent columns subscribed the consented rows with their original timestamps, and the catch-up pass picked up exactly the changed profiles.

## Phase 4: Back in Stock

- `klaviyo bis export`: read Back in Stock events with profile email, take SKU from the event, keep latest per email + SKU, apply `--since`, write STOQ template columns (`bis.csv`), `bis.reference.csv` (CA IDs and names) and `bis.excluded.csv` with reasons.

**Trial:** a 10-row file in the export's format, using the dev store's SKU and our test addresses (never real customers), uploaded by hand in the dev store's STOQ admin. Checks the file format, including that `dd/mm/yyyy` dates are read day-first.

**Gate:** the 10 rows appear in STOQ Reports → Current waitlist with the right SKUs and no customer is messaged. A sample of the real `klaviyo_ca` export checked against the matching Klaviyo events, and every excluded row has a reason.

## Phase 5: README and sign-off

- README: setup (`uv`, `.env`), every command and flag with an example, the recommended run order for the migration, STOQ CSV preparation and the STOQ admin upload, the catch-up run, deleting local data afterwards, and warnings (import overwrites matching profiles; suppressions override newer US subscribes; `Phone` is left blank in the STOQ file on purpose).
- Walk through every acceptance criterion in the spec and record the result.

**Gate:** all acceptance criteria in `REQUIREMENTS.md` checked off.

## Phase 6: Klaviyo US → Shopify US email consent sync

Specified by `docs/CONSENT_SYNC.md`: decisions D1–D14 and the pilot results in section 9.8. Email marketing consent only. Klaviyo is the source of truth, except for the 26 D14 customers.

### Commands

- **`migtool shopify consent-plan --klaviyo <profiles export> --shopify <customers export>`**. Local and read-only; no API calls.
  - Joins the two exports by lowercased email and applies the mapping in `CONSENT_SYNC.md` 4.1.
  - Writes `<run>.plan.csv`, one row per customer to write: Shopify customer ID, email, Klaviyo profile ID, Klaviyo state, Shopify state now, target state (`SUBSCRIBED` or `UNSUBSCRIBED`), target date, and whether Shopify's date is newer (D11).
  - Writes `<run>.klaviyo_d14.csv`, the D14 customers in the dedupe layout (`Email Marketing Consent` = Subscribe, `Email Marketing Consent Timestamp` = Shopify's `consentUpdatedAt`).
  - Writes `<run>.excluded.csv`: D12 and `INVALID`/`REDACTED`, with reasons.
  - Prints counts by transition. `NOT_SUBSCRIBED` is never a target.
  - Target date = D5: `ca_consent_timestamp` (or `ca_suppression_timestamp` for suppressed profiles) on migrated CA profiles, Klaviyo's consent or suppression date otherwise. A date in the future is set to the plan time (P8a), and a missing date is reported.
- **D14 Klaviyo update:** the existing `klaviyo dedupe import` with a new role **`consent`**: update existing profiles only, consent only, no migration tags, no join list. It subscribes with `--subscribe-list Xz4KGg` (LOF USA Newsletter - Main, D15; the Welcome Series may follow) and a custom source of "Shopify email consent (consent sync)". The custom source becomes an option of the writer; today it's the constant `migtool CA migration`. Checked with `klaviyo dedupe check --role consent`.
- **`migtool shopify consent-sync --to shopify_us --plan <plan.csv> [--limit N] [--resume] [--yes]`**. The only Shopify write.
  - Before anything: `check_shop` (the token belongs to Shopify US), a check that the token has `write_customers`, and a typed confirmation naming the store and the number of writes. `--yes` skips only the typed confirmation.
  - For each batch of up to 50 customers: read their current `emailMarketingConsent` with one `nodes(ids:)` query; **skip customers already in the target state** (P7). Otherwise send `customerEmailMarketingConsentUpdate` with `marketingState`, `marketingOptInLevel: SINGLE_OPT_IN` and `consentUpdatedAt` = the target date.
  - Results per customer go to `<run>.results.csv`: written, skipped (already in the target state), refused (with the `userErrors`), or unknown (lost response, read back and recorded).
  - Mutations are **never retried** blindly. A lost response is resolved by reading the customer back.
  - Cost-based throttling as for queries (`THROTTLED` waits for the bucket). The achieved rate is printed, so the canary measures it. If it's too slow for ~261k writes, the fallback is `bulkOperationRunMutation` (a JSONL upload), as a separate decision.
  - `--limit N` (canary); `--resume` continues from a checkpoint in `state/` (customers already processed are skipped, and the skip-if-already rule makes re-running safe anyway).
  - Implemented as one new `ShopifyClient` method, `update_email_consent(...)`. The client's mutation guard stays: `query()` still refuses mutations, and only `start_bulk_export` and `update_email_consent` send them.
- **`migtool shopify consent-validate --klaviyo <export> --shopify <export> [--plan <plan.csv> --results <results.csv>] [--klaviyo-before <backup>]`**. Local and read-only.
  - State: every matched customer's Shopify state equals the mapped target; D12 counts as a match; `INVALID`/`REDACTED` are listed separately; D14 customers are subscribed on both sides.
  - Dates: compared only where the run changed the state (P8b), so Shopify's date must equal the plan's. For D14, Klaviyo's date must equal Shopify's.
  - Klaviyo before/after (with `--klaviyo-before`): no consent state, date or method changed between the pre-run backup and the post-run export, except D14 customers and normal customer activity. Changes are listed for review, as in the Sep 28 three-way comparison.
  - Writes `<run>.mismatches.csv` and counts, and exits 1 on any mismatch.

### Tests

- Mapping: one test per row of `CONSENT_SYNC.md` 4.1, including suppressed vs `UNSUBSCRIBE`-only, D12, D14 and `INVALID`/`REDACTED`; `NOT_SUBSCRIBED` never planned; target dates (migrated vs US, suppressed, future dates clamped, missing dates).
- Sync: skip-if-already-in-target; `userErrors` recorded as refused, and the run continues; lost response read back, not retried; `--limit`; `--resume`; refused without `write_customers` or with the wrong store; the client still refuses any other mutation.
- Validate: matching and mismatched states, D12, D14 dates, date checks only for changed customers, Klaviyo before/after detection.
- D14 role: consent only, no tags, existing profiles only, custom source sent.

### Trial

- The six pilot test accounts (`nick+consentpilot1–6@0xb8.net`), reset by the user to new starting states, run through `consent-plan` → `consent-sync` → `consent-validate` with a plan restricted to them. A D14-style case on one account goes through the `consent` role.

### Gate

- All tests pass. The trial validates clean. Apart from the D14 case, there are no Klaviyo changes, no list additions and no emails, and Attentive is unchanged (user check). The D14 case is allowed to subscribe its profile to LOF USA Newsletter - Main and may trigger the Welcome Series (D15).
- The user approves the refreshed plan counts before the run.

### Run order

See `CONSENT_SYNC.md` 5.4 and tasks #10/#11:

1. Full Klaviyo US backup and a fresh Shopify export, with both integration settings normal.
2. `consent-plan` on those exports; the user approves the counts.
3. D14: `dedupe import --role consent`, then wait for the back-dated subscribes to apply (10–20 minutes) and run `dedupe check`.
4. The user points the subscriber sync at NK_consentsync and turns "Sync Klaviyo profiles to Shopify" off.
5. **Canary:** `consent-sync --limit` with about 100 subscribe and 100 unsubscribe writes. Wait 15 minutes, then check NK_consentsync, those customers' Klaviyo consent, and any emails. Continue only if all are clean.
6. `consent-sync --resume` for the rest.
7. The user restores both settings; any real signups on NK_consentsync move to Main.
8. Post-run exports, `consent-validate` (including Klaviyo before/after), then a run record in `exports/consent_sync/`.
9. The user removes the write scopes from the Shopify token.

## Migration run order (after sign-off)

1. Klaviyo: create the LOF Canada Newsletter list in `klaviyo_us`. Export profiles from `klaviyo_ca` and `klaviyo_us`; dedupe outside the tool (including phone uniqueness and removing overlapping Shopify properties); import unique CA profiles into `klaviyo_us` with `--list-id` set to the LOF Canada Newsletter list.
2. Klaviyo: export CA suppressions; choose which to apply by editing the file. Pilot one address first (`suppressions import --limit 1`), then run `suppressions check` on it until it shows as suppressed (this took two to four hours in the sandbox). Then import the rest and check them the same way. If the pilot never applies, import with `--as-unsubscribe` and apply true suppression in the Klaviyo UI.
3. Klaviyo: export lists and segments from `klaviyo_ca`; attach chosen sets of profiles to US lists with `lists add` or through the Klaviyo UI (often into existing US lists); clone segments in the Klaviyo UI.
4. Back in Stock: export from `klaviyo_ca`; review the CSV, remove signups from unsubscribed or never-subscribed people (STOQ's Klaviyo integration may subscribe everyone uploaded), and fill `Market`, `GDPR confirmed` and inventory-location data; upload it in STOQ admin on the US store.
5. Catch-up run, immediately before CA Klaviyo sign-ups are turned off: repeat steps 1–4 with `--since` set to the start of the main run (the suppressions export is required: unsubscribes don't move a profile's `updated` time, so only it catches them); dedupe the delta files; re-import.
6. Delete `exports/` (except the kept snapshot in `exports/og_exports/`) and `state/`.

## Risks

| Risk | Mitigation |
|---|---|
| Klaviyo may not expose segment rules in a usable form | Found in phase 1; if so, labels become a manual column and the spec is updated |
| Back in Stock events may lack a SKU | Such rows go to the excluded file with a reason; volume reported in phase 1 |
| Klaviyo import overwrites matching profiles | Dedupe happens beforehand (outside the tool); README warning; trial first |
| Catch-up run misses a change `--since` can't see | Phase 1 confirms which changes move the `updated` time; set `--since` a little before the main run started, since every write is safe to repeat |
| Consent sync writes Klaviyo history back through Shopify | Pilot found no feedback; dummy list and canary batch on run day; Klaviyo before/after comparison in validation |
| Shopify write throughput too low for ~261k writes | Canary measures the rate; bulk mutation as a fallback |
| Rate limits make runs long | Pacing; `--resume` on exports; imports are safe to re-run; expected run times documented in the README |
