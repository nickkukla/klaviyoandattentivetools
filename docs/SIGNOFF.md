# Sign-off

Each acceptance criterion from `docs/REQUIREMENTS.md` ("Done when"), with its result and evidence. Write trials ran in `klaviyo_sandbox` (`T2aEdf`) only; nothing was written to `klaviyo_ca` or `klaviyo_us` during development. Details of every API behaviour are in `docs/API_NOTES.md`.

Status: ✅ met · ⚠️ met, with a qualification · ⏳ pending

| # | Criterion | Status |
|---|---|---|
| 1 | Klaviyo profile import | ⚠️ |
| 2 | Klaviyo suppressions | ⚠️ |
| 3 | Klaviyo list add | ✅ |
| 4 | Catch-up run | ✅ |
| 5 | Klaviyo segments | ✅ |
| 6 | Back in Stock export | ✅ |
| 7 | STOQ | ✅ (upload reported by the user) |
| 8 | README | ✅ |

## 1. Klaviyo profile import ⚠️

> A 10-profile trial (`--limit 10`) using addresses we control. Re-exporting them shows the same consent status and timestamp, `ca_consent_*` properties are set, and a suppressed profile stays suppressed.

Trial on 2026-09-24 (run `20260924T224403Z-912e`): a 10-profile file (`test04`, `test06`–`test11`, `test13`–`test15@0xb8.net`) plus one row with no email. The file had exactly 10 profiles, so `--limit` wasn't needed; `--limit` is covered by unit tests and was used in the `lists add` trial.

- **Consent status:** matched the file for every profile. Suppressed rows were blocked (with `--as-unsubscribe`; true suppression was verified separately, see 2). `test08`, which was subscribed in the sandbox, became unsubscribed, so suppression wins. Never-subscribed rows were unchanged. The row with no email was skipped with its reason.
- **Consent timestamp:** matched for profiles not already subscribed in the destination (`test06` 2022-06-06, `test10` 2021-10-10, `test13` 2023-01-13). Two Klaviyo behaviours qualify this, and neither can be changed through the API:
  - A profile **already subscribed** keeps its existing timestamp (`test04`: file 2019, kept 2025).
  - **Unsubscribes** are stamped with the import time. The original date is stored in `ca_suppression_timestamp`.
- **Properties:** `ca_external_id`, `ca_consent_method`, `ca_consent_source`, `ca_suppression_reason`, `ca_suppression_timestamp`, `migrated_from=ca` and `migration_run_id` were set. The destination `external_id` was untouched (`test04` kept `US-04`). Property types survived (number, text with a leading zero, list), and `$consent` was not sent.
- **No emails:** there were no `Received Email` events on any trial profile, including those subscribed into a double-opt-in list.
- Re-export with `profiles export --since` on 2026-09-25 confirmed the properties and `external_id`. By then `test07`–`test10` showed as subscribed. The STOQ trial upload did that (see 7), not the import; the import results above were recorded before it.

## 2. Klaviyo suppressions ⚠️

> A 5-row trial appears in the destination's suppression list, including one profile that was subscribed there.

The trial was `suppressions import` of `test01`, `test02`, `test03`, `test05` and a new `test12`, submitted at 22:44 UTC on 2026-09-24.

- **Suppression does apply in the sandbox, but slowly.** Jobs applied about four hours after submission, while the job status stayed `processing` and reported every profile as skipped. So the import no longer waits on the jobs, and `suppressions check` confirms the result.
- **Subscribed profiles:** `test01` and `test02` were subscribed in the sandbox and are now `USER_SUPPRESSED` (from jobs with the same addresses submitted earlier), with `can_receive_email_marketing = false`. That meets "including one that was subscribed".
- **`suppressions check` at 02:50 UTC on 2026-09-25** (4h06m after the import): `test01` and `test02` were suppressed. `test03` and `test05` weren't: the STOQ trial upload had re-subscribed them (and unsuppressed `test05`, see 7).
- **Missing profile:** `test12` was created and tagged by the import, but its suppression result is **inconclusive**. The review-fix trials re-subscribed `test12` before four hours had passed, and Klaviyo logs no event when a bulk suppression applies, so the order can't be told apart. The job still reads `processing`, total 5, skipped 5.
- **Qualification:** the criterion (a trial row, including a subscribed profile, appears in the suppression list) is met. "Creates a missing profile and suppresses it" wasn't cleanly shown in this trial; the Phase 1 job did create `test06` and suppress it about four hours later.
- **In the real migration,** the one-address pilot plus `suppressions check` confirms how long it takes in `klaviyo_us`.

## 3. Klaviyo list add ✅

> A 5-row trial adds the profiles to a list without changing their consent; running it again changes nothing.

- **Email only** (`test01`, `test04`–`test06`, new `test07`): all five were added, and nobody's consent changed. `test07` was created and tagged; existing profiles weren't tagged. The CA profile ID in the file wasn't sent. The second run changed nothing.
- **With consent columns** (`test02`, and new `test08`–`test11`): only valid subscribed rows were subscribed, with their original timestamp (`test08` 2022-08-08). Unsubscribed, never-subscribed and hard-bounced rows were added without being subscribed.

## 4. Catch-up run ✅

> In `klaviyo_sandbox`, records changed after a `--since` timestamp are exported and re-imported, and nothing older is included.

After changing `test13` (a property), `test15` (a subscribe) and `test10` (an unsubscribe):
- `profiles export --since` returned exactly `test13` and `test15`, out of about 153k sandbox profiles.
- `suppressions export --since` returned exactly `test10`. Unsubscribes don't move a profile's update time, so the suppressions export is part of every catch-up.
- Re-importing both files applied exactly those changes.

`--since` was also verified on `klaviyo_ca` (read-only) for profiles (39,761), suppressions (4,237) and lists (3,641). Each returned only records after the cutoff, and nothing that the full exports showed as changed after it was missing.

## 5. Klaviyo segments ✅

> Labels are spot-checked by hand against five segments with known rules.

`segments export` on `klaviyo_ca` covered 53 segments and 1,273,202 memberships. Five segments were checked against their rule definitions:

| Segment | Rules | Labels |
|---|---|---|
| All Leads – All Time (No Purchase) | Subscribed to List, Checkout Started (Shopify), Placed Order = 0 | engagement, site activity |
| RSVPd to Eventbrite L30 Days | Bought Ticket (Eventbrite) | third-party |
| engaged last 365 days | Opened/Clicked Email, Viewed Product and Active on Site (API) | engagement, site activity |
| Subscribed To Back In Stock – Splash Stripes | Subscribed to Back in Stock | none |
| Toronto Store Audience | postal-code distance, city, email consent | none |

## 6. Back in Stock export ✅

> A sample of rows checked against the matching Klaviyo events, with correct SKUs, and every excluded row has a reason.

`bis export` on `klaviyo_ca` read 43,445 signups and wrote 40,015 rows (5,214 SKUs). 3,430 were excluded, each with a reason: 3,423 older signups for the same email and SKU, and 7 profiles without an email. 25 random rows matched their Klaviyo events on SKU, email, date and consent.

## 7. STOQ ✅

> A 10-row export file, edited to the dev store's SKUs and uploaded in the dev store's STOQ admin, appears in STOQ Reports → Current waitlist with the right SKUs, and no customer is messaged.

`exports/stoq_dev_trial/bis_trial_10.csv` was uploaded to the dev store on 2026-09-25 at about 01:09 UTC. It used the export's column layout, the dev-store SKU `LWLB0243-0268-L` and test addresses `test01`–`test10@0xb8.net`, never real customers. The user reported that it uploaded without errors. The dates were chosen so that a day/month mix-up would fail (`13/01/2026`).

Finding: STOQ's Klaviyo integration then subscribed six of the uploaded addresses in `klaviyo_sandbox`, including rows marked `Accepts marketing = false` and one suppressed profile. No `Received Email` events were seen. The user will remove unsubscribed and never-subscribed people from the file before the real upload (README, run order).

## 8. README ✅

> Every command and flag is documented with an example.

All 11 commands and every flag have a description and at least one example. This was checked by a script that walks the CLI's own command definitions and looks for each one in its README section. The README also covers setup, key scopes, write safety, output files, the run order, STOQ preparation and upload, the catch-up run, warnings, deleting local data and troubleshooting.

## Review (2026-09-25)

An independent review (Codex) found failure-path and data-fidelity issues. A second review of those fixes found six more, including a regression (a job accepted before a later failure could go unrecorded), and a third found three more (long error responses truncated before parsing, refused rows logged late, non-finite numbers inside JSON cells). All were fixed with tests, and the failure paths were re-tried in `klaviyo_sandbox` (see the change history in `docs/REQUIREMENTS.md` and `docs/API_NOTES.md`). One concern from the first review, that adding profiles to a list before the historical subscribe could trigger list flows, is resolved operationally: every destination flow is gated on profile triggers that exclude CA members.

## Open before the migration run

- Re-export CA profiles with the current version before importing (typed property columns).
- Legal review of consent transfer (CASL, PIPEDA), per `docs/REQUIREMENTS.md`.
- Give the `klaviyo_us` key write scopes only when the migration run starts.
