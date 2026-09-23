# Build plan

This plan builds what `docs/REQUIREMENTS.md` specifies. Phases are ordered by dependency and risk: shared foundations first, then read-only checks against the real APIs, then exports (safe), then writes (risky), with Attentive last because it waits on outside inputs.

Each phase ends with a **gate**: the checks that must pass before the next phase starts. Gates use the acceptance criteria in the spec.

## Layout

```
pyproject.toml            # uv project; console script `migtool`
.env.example              # every variable, no values
src/migtool/
  cli.py                  # typer app; sub-apps: klaviyo, attentive, stoq
  config.py               # instance registry + .env loading
  http.py                 # shared client: retries, Retry-After, rate limiting
  safety.py               # --to / confirm prompt / --yes / --allow-write-to-source
  output.py               # exports/<instance>/<object>/<ts>.*, manifest.json
  runlog.py               # errors.csv, run summary, exit code
  state.py                # resume checkpoints, saved job IDs (state/)
  klaviyo/  client.py profiles.py suppressions.py lists.py segments.py bis.py
  attentive/ client.py segments.py
  stoq/     client.py importer.py
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

- `uv` project, `typer` CLI skeleton with `klaviyo`, `attentive` and `stoq` sub-commands, `pytest` + `respx` for recorded-response tests.
- **Config:** instances `klaviyo_ca`, `klaviyo_us`, `klaviyo_sandbox`, `attentive_ca`, `attentive_us`, `stoq_dev`, `stoq_us`, each mapped to its `.env` variable. A missing variable is a clear error naming the variable. Keys never appear in logs or errors.
- **HTTP client:** retries 429 and 5xx up to 6 times with increasing waits, honouring `Retry-After`. Per-endpoint rate limiter (Klaviyo publishes burst and steady limits per endpoint; STOQ uses 360 points per minute).
- **Safety:** `--to` required on writes; prompt shows account, instance and record count; typed confirmation; `--yes`; `--allow-write-to-source` for `_ca` targets.
- **Output:** CSV and JSONL writers, `manifest.json` with counts per run.
- **Run log:** `<run>.errors.csv`, end-of-run summary (read, written, skipped, failed), non-zero exit on any failure.
- **State:** checkpoint files for `--resume`; saved bulk job IDs.

**Gate:** unit tests pass for retries (fake 429 with `Retry-After`), rate limiter pacing, the confirmation prompt (including a refused `_ca` write), and resume from a checkpoint.

## Phase 1: API checks (read-only, safe in production)

Confirm the facts the design depends on before building features. Record results in `docs/API_NOTES.md`, and update the spec if anything contradicts it.

| Check | Why it matters |
|---|---|
| `whoami` against every configured instance | Keys work and point at the expected accounts |
| Attentive: does List Segments include segments built in the UI, or only API-created ones? | Whether the segment export is complete, and how the duplicate guard behaves |
| Klaviyo: which API revision returns segment rule definitions, and their shape | Segment labels (engagement / site activity / third-party) depend on it |
| Klaviyo: map metric IDs to their source integration | Same |
| Klaviyo: name of the Back in Stock metric in `klaviyo_ca`, and the event property keys holding variant and product IDs | Back in Stock export |
| Klaviyo: catalog variants expose SKU and inventory quantity | SKU lookup and in-stock filtering |
| Klaviyo: batch limits and request shape for bulk profile import, historical-import subscribe, and bulk suppression | Import design |
| Klaviyo: which suppression reasons the profile data reports | Filtering hard bounce / spam / manual vs. unsubscribe |
| STOQ: v1 intents on the dev store — response when a signup already exists, and whether it counts against the 360-point limit | Duplicate handling and pacing |

Record real responses (secrets and personal data scrubbed) as test fixtures while doing this.

**Gate:** every check answered in `API_NOTES.md`. Any change to the spec agreed with the user.

## Phase 2: Klaviyo exports (read-only)

- `klaviyo profiles export` with the full CSV layout from the spec, `--resume`, `--with-predictive`, `--segment`.
- `klaviyo lists export` → `lists.csv`, `list_members.csv`.
- `klaviyo segments export` → `segments.csv` with the three label columns and event names, `segment_members.csv`.
- `klaviyo suppressions export`, filtered to hard bounces, spam complaints and manual suppressions.

**Gate:**
- Full profile export of `klaviyo_ca` completes (about 636k rows); totals in `manifest.json` match the dashboard (active, suppressed, never subscribed).
- An export interrupted mid-run finishes correctly with `--resume`, with no duplicate or missing rows.
- Segment labels spot-checked against five segments with known rules.

## Phase 3: Klaviyo writes

- `klaviyo profiles import`: bulk profile import, then historical-import subscribe (to `--list-id`) using the original consent timestamp, then unsubscribe/suppress where the CSV says so. Writes `ca_consent_method`, `ca_consent_source`, `ca_suppression_reason`, `ca_suppression_timestamp`. Supports `--limit`.
- `klaviyo suppressions import`.
- Both track Klaviyo's background jobs and put per-record errors in the errors file.

**Trial** in `klaviyo_sandbox` if granted, otherwise in `klaviyo_us` against the dedicated test list:
- 10 profiles on addresses we control, mixing subscribed, unsubscribed, suppressed and never-subscribed.
- 5 suppressions.

**Gate:** re-exporting the trial profiles shows the same consent status and timestamp, the `ca_*` properties are set, suppressed profiles are still suppressed, never-subscribed profiles are unchanged, no welcome or double opt-in emails were sent, and the 5 suppressions appear in the suppression list.

## Phase 4: Back in Stock and STOQ

- `klaviyo bis export`: read Back in Stock events with profile email, look up SKU via the catalog, keep latest per email + SKU, skip in-stock variants, apply `--since`, write STOQ template columns, write `bis.excluded.csv` with reasons.
- `stoq import`: v1 intents, paced to the rate limit, duplicates counted separately, `--resume`.

**Trial:** a 10-row CSV using the dev store's SKUs into `stoq_dev`.

**Gate:** the 10 rows appear in STOQ Reports → Current waitlist with the right SKUs and no customer is messaged. A sample of the real `klaviyo_ca` export checked against the matching Klaviyo events, and every excluded row has a reason.

## Phase 5: Attentive segments

Can be built any time after phase 0. Running it for real waits on the segment CSVs from Attentive's customer success manager and on the subscriber migration being finished.

- `attentive whoami`.
- `attentive segments export`.
- `attentive segments upload`: validation, rejected-rows file, suffix naming, duplicate-name guard, dry-run, `--segment` pilot, batches of 10,000.
- `attentive segments jobs`: status, results download, skipped counts and `<segment>.skipped.csv`.

**Trial (production, there is no sandbox):** dry-run on every CSV, then a real pilot on one small segment in `attentive_us`.

**Gate:** the pilot job completes and its results file shows no failures other than expected skips. Re-running the same upload is stopped by the duplicate-name guard.

## Phase 6: README and sign-off

- README: setup (`uv`, `.env`), every command and flag with an example, the recommended run order for the migration, STOQ CSV preparation and the admin-upload fallback, and warnings (import overwrites matching profiles; Attentive upload is one-time).
- Walk through every acceptance criterion in the spec and record the result.

**Gate:** all acceptance criteria in `REQUIREMENTS.md` checked off.

## Migration run order (after sign-off)

1. Klaviyo: export profiles from `klaviyo_ca` and `klaviyo_us`; dedupe outside the tool; import unique CA profiles into `klaviyo_us`.
2. Klaviyo: export CA suppressions; import into `klaviyo_us`.
3. Klaviyo: export lists and segments from `klaviyo_ca` for the record; clone segments in the Klaviyo UI.
4. Back in Stock: export from `klaviyo_ca`; review the CSV; `stoq import --to stoq_us`.
5. Attentive: after the subscriber migration is finished and the CSM's CSVs arrive — export segments, dry-run every CSV, pilot one, upload the rest, check jobs.

## Risks

| Risk | Mitigation |
|---|---|
| Attentive Segments APIs are beta | Pilot one segment first; fall back to the manual UI process named in the spec |
| No Attentive sandbox | Dry-run on every file, one-segment pilot, duplicate-name guard |
| Klaviyo may not expose segment rules in a usable form | Found in phase 1; if so, labels become a manual column and the spec is updated |
| Back in Stock events may lack variant IDs, or SKUs may be missing from the catalog | Such rows go to the excluded file with a reason; volume reported in phase 1 |
| Klaviyo import overwrites matching profiles | Dedupe happens beforehand (outside the tool); README warning; trial first |
| Rate limits make runs long | Pacing plus `--resume`; expected run times documented in the README |
