# Klaviyo US → Shopify US consent sync (plan, not started)

Status: **planned; pilot passed (2026-10-02).** The build is specified in `docs/BUILD_PLAN.md` (phase 6). No production writes until the command is built, reviewed and the run is started by the user.

## 0. Decisions so far (2026-10-02)

| # | Decision |
|---|---|
| D1 | **Email marketing only.** No SMS consent changes are made (Klaviyo has no SMS consent state; see 2.3). The client is told this. |
| D2 | Klaviyo **never subscribed**, Shopify `SUBSCRIBED`: **superseded by D14.** The pilot showed the API can't set `NOT_SUBSCRIBED` ("Cannot specify NOT_SUBSCRIBED as a marketing state input"). Klaviyo never subscribed + Shopify `NOT_SUBSCRIBED` remains a match. |
| D3 | Klaviyo **suppressed** (bounce, spam complaint, manual) → Shopify **`UNSUBSCRIBED`**, whatever Shopify shows now. |
| D4 | `write_customers` was added **in place to the single existing Shopify token** (2026-10-02). migtool's Shopify client still refuses every mutation except the bulk-export start and the one consent mutation added for this job (phase 6). The user removes the write scopes after the sync (task #11). |
| D5 | Shopify's consent date is set to the **original** consent date: `ca_consent_timestamp` / `ca_suppression_timestamp` for migrated CA profiles, Klaviyo's own date otherwise. |
| D6 | **Shopify-newer conflicts:** count them before deciding (section 5.1). Nothing changes until there's a decision. |
| D7 | Klaviyo app settings in Shopify US (read by the user): *From Shopify*: "Sync Shopify email subscribers to Klaviyo" **on**, into list **LOF USA Newsletter - Main** (Xz4KGg); SMS sync on but inactive (texting not set up). *To Shopify*: "Sync Klaviyo profiles to Shopify" **on**, existing Shopify customers only; it creates no new customers. |
| D8 | **Full Klaviyo US backup** (all ~815k profiles) immediately before any change. |
| D9 | **Final (2026-10-02): Option B.** During the run, "Sync Klaviyo profiles to Shopify" is off (D13) and "Sync Shopify email subscribers to Klaviyo" stays **on**, pointed at the dummy list **NK_consentsync (VhVV8j)** in Klaviyo US (empty, no flows). Real signups keep reaching Klaviyo during the run; any feedback from our writes is contained and visible on the dummy list. Start with a **canary batch** of subscribe and unsubscribe writes, wait 15 minutes, check the dummy list and those customers' Klaviyo consent, and continue only if nothing changed. Afterwards, restore Main and move real signups from the dummy list to Main. Rejected: A (both directions off: a gap for real customers' consent changes needing a catch-up) and C (Main list: list additions would trigger the Welcome Series). |
| D10 | Klaviyo **unsubscribed**, Shopify `NOT_SUBSCRIBED` → write **`UNSUBSCRIBED`** (Q12). |
| D11 | **Overwrite all** Shopify-newer conflicts, including the 165 re-subscribes (Q5): Klaviyo is the source of truth. |
| D12 | Klaviyo **never subscribed**, Shopify `UNSUBSCRIBED`: **left as `UNSUBSCRIBED`**, because `NOT_SUBSCRIBED` can't be set (pilot). Validation counts these as matching. |
| D13 | The user turns **"Sync Klaviyo profiles to Shopify" off** for the duration of the run and back on afterwards. Both steps are on the run checklist. The pilot confirmed that with it off, Klaviyo changes don't reach Shopify. |
| D14 | Klaviyo **never subscribed** (not suppressed), Shopify **`SUBSCRIBED`** (26 on 2026-10-02): treated as real checkout opt-ins that never reached Klaviyo. **Shopify is kept**, and **Klaviyo is updated to subscribed** with Shopify's `consentUpdatedAt` as the consent date, via a back-dated subscribe with a custom source of "Shopify email consent (consent sync)" (Klaviyo shows the method as API). This is the only change to Klaviyo, and it runs **before** the Shopify writes, after which both sides match. Validation: both subscribed, Klaviyo's date = Shopify's. |
| D15 | The D14 Klaviyo subscribes go into **LOF USA Newsletter - Main (Xz4KGg)** (user, 2026-10-02). They may receive the Welcome Series, which the user accepts. The Welcome flows' "Placed Order = 0" filter will exclude any who have ordered. |
| D16 | **Shopify silently ignores a consent change dated before the customer's current consent date** (no error; state and `updatedAt` unchanged; found in the phase 6 trial, 2026-10-02). Where Shopify's date is newer than or equal to Klaviyo's original date (or there's no date), the write is **dated at the sync time** (user, option A), so Klaviyo's state still wins (D11). Klaviyo keeps the original date. On 2026-10-02 data: **506** writes. `consent-sync` re-checks the live date at write time and records any change Shopify didn't apply as **ignored** (unresolved). |


## 1. The request (as stated by the user, 2026-10-02)

- The client wants Shopify US customers' marketing consent to match Klaviyo US, which is the source of truth and has been the primary consent tool. Shopify's consent has gone stale.
- Only the consent fields change: email marketing (admin/CSV name "Accepts Email Marketing") and SMS marketing ("Accepts SMS Marketing"), each with its consent date. Nothing else on the customer changes.
- Only Klaviyo profiles with a matching Shopify customer are in scope, not all ~815k profiles.
- Where the dates conflict (Shopify's consent date is newer than Klaviyo's), the user's position is that Klaviyo wins and Shopify is overwritten. To be confirmed as part of the plan.
- After the sync there must be a way to verify, record by record, that Klaviyo and Shopify agree.
- SMS needs its own analysis. Attentive has managed SMS recently, so Shopify's SMS consent may be current even where email isn't.
- Before anything runs, pilot with test accounts whether Shopify consent changes flow on into Attentive. Attentive isn't reachable by API, so the pilot is the only way to know.

## 2. Findings from existing data (read-only, local exports)

Sources: Klaviyo US export after the Shopify import (`exports/mainrun/klaviyo_us/post_shopify/20260928T021946Z.csv`, 814,332 profiles), the pre-migration US export (`…/profiles/20260925T153516Z.csv`), the Shopify US customer bulk export (`exports/shopify_us/customers-export/20260928T031805Z.csv`, 641,838 customers), and the dedupe import files. All of these are from Sep 25–28, so the numbers need refreshing before the run.

### 2.1 Does an edit in Klaviyo push anything to Shopify?

| Klaviyo change (Sep 26 import) | Shopify customers | Pushed to Shopify? |
|---|---|---|
| **Property only**: 01 set `migration_hold` on 72,897 US profiles, 02:39–02:59 UTC | 51,305 (not tagged by the CA import) | **No.** Only 60 customers (0.1%) show an update in that window, and only 2 a consent-date change. 34,497 (67%) haven't changed at all since before Sep 25. The untouched control group looks the same. |
| **Unsubscribe**: 04b, 05:03–05:16 UTC | 7,157 | **Yes.** 504 consent dates fall in the run window, and 877 of the 910 US subscribers 04b unsubscribed are now not subscribed in Shopify. |
| **Subscribe with a historical date** (`historical_import`): 04a | 15,894 | **No.** 13,564 that went from never subscribed to subscribed in Klaviyo are still `NOT_SUBSCRIBED` in Shopify, with no consent date. |

Conclusions:

- Setting a property such as `shopify_sync=true` **won't** trigger a sync. Klaviyo's integration doesn't write property edits to Shopify.
- Klaviyo does write some consent **changes** (unsubscribes) to Shopify, but not historical subscribes. So "touching" profiles in Klaviyo, even through consent, can't reliably bring Shopify in line, and a re-subscribe would also rewrite Klaviyo's own consent dates.
- The sync therefore needs to write Shopify directly (section 4).

### 2.2 Scope: how many profiles match

| | Count (Sep 28) |
|---|---|
| Klaviyo US profiles | 814,332 |
| Shopify US customers | 641,838 (508,071 with an email; 130,377 phone-only) |
| Klaviyo profiles with a Shopify customer by email | **508,065** |
| … whose email consent differs (subscribed in one, not the other) | **147,086** (72,875 Shopify yes / Klaviyo no; 74,211 Shopify no / Klaviyo yes, of which 54,180 are migrated CA customers whose Shopify consent the import left blank) |

So the matched set is about 508k (not ~300k). Under the agreed mapping, the fresh dry run (section 4.1, 2026-10-02) gives **261,017** Shopify writes plus 26 Klaviyo writes (D14); the rest already match. That count is higher than the simple subscribed/not-subscribed split above because suppressed profiles and never-subscribed cases are treated separately.

### 2.3 SMS

- **Klaviyo isn't an SMS consent source here.** Our Klaviyo exports only carry email consent. What Klaviyo has for SMS is Attentive's properties: `sms_attentive_signup=true` on 219,908 profiles, `smsTimeStamp` on 226,245 and `$sms_consent_method` on 337,616. These record that someone signed up through Attentive, not their current status (there's no unsubscribe flag).
- **Shopify SMS state** (641,838 customers): 66,242 `SUBSCRIBED`, 100,041 `UNSUBSCRIBED`, 80,180 `NOT_SUBSCRIBED`; 395,374 have no phone, so no SMS state.
- Klaviyo's Attentive-signup flag compared with Shopify's SMS state, for matched profiles:

  | | Shopify SUBSCRIBED | UNSUBSCRIBED | NOT_SUBSCRIBED | no phone in Shopify |
  |---|---|---|---|---|
  | Attentive signup | 21,717 | 41,461 | 19,847 | 71,803 |
  | no Attentive signup | 4,714 | 5,277 | 23,070 | 320,175 |

  41k Attentive signups show `UNSUBSCRIBED` in Shopify. These may be real opt-outs that flowed from Attentive into Shopify, which would make Shopify's SMS consent **fresher** than anything in Klaviyo.
- **Working conclusion:** Klaviyo has no authority for SMS consent, so the sync should be **email only**. SMS stays as it is in Shopify unless an Attentive export shows otherwise (open question Q2).

## 3. Shopify fields involved (to confirm in the pilot)

| Admin / CSV name | Admin API (GraphQL) | Values |
|---|---|---|
| Accepts Email Marketing | `emailMarketingConsent.marketingState` | `SUBSCRIBED`, `NOT_SUBSCRIBED`, `UNSUBSCRIBED`, `PENDING`; `REDACTED` and `INVALID` are read-only |
| (email opt-in level) | `emailMarketingConsent.marketingOptInLevel` | `SINGLE_OPT_IN`, `CONFIRMED_OPT_IN`, `UNKNOWN` |
| (email consent date) | `emailMarketingConsent.consentUpdatedAt` | timestamp |
| Accepts SMS Marketing | `smsMarketingConsent.marketingState` | as for email; needs a phone number on the customer |
| (SMS opt-in level, date, source) | `smsMarketingConsent.marketingOptInLevel`, `.consentUpdatedAt`, `.consentCollectedFrom` | `consentCollectedFrom` is `SHOPIFY` or `OTHER` (set by an app) |
| (legacy) | `acceptsMarketing`, `acceptsMarketingUpdatedAt` | deprecated mirror of email consent; not written directly |

These are the full set of customer-level marketing consent fields. The writes are two dedicated mutations, `customerEmailMarketingConsentUpdate` and `customerSmsMarketingConsentUpdate`, which change consent and nothing else on the customer. Both need the `write_customers` scope. Still to verify in the pilot:

- whether `consentUpdatedAt` can be set to a past date
- which state changes Shopify allows (for example `SUBSCRIBED` → `NOT_SUBSCRIBED`)
- what Shopify does on a no-op

## 4. Mechanism options

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Direct Shopify write (recommended)** | migtool computes the diff from fresh exports and sends `customerEmailMarketingConsentUpdate` only for customers that differ (or in bulk via `bulkOperationRunMutation`) | Touches only the consent fields and only the ~147k that differ; dry run, pilot, `--limit`, resumable, per-row results, then a full comparison | Needs a token with `write_customers`, which breaks the read-only rule for the current token (Q4) |
| B. Klaviyo "touch" | Edit profiles in Klaviyo and let the integration push | No Shopify token | **Shown not to work** (2.1): property edits don't sync, historical subscribes don't sync, re-subscribing rewrites Klaviyo consent |
| C. Shopify customer CSV import | Admin CSV import with "Accepts Email Marketing" | No API token | Can't set consent dates; overwrites every column in the file, so other customer data is at risk; poor validation; no per-row results |

## 4.1 Mapping and counts (dry run, 2026-10-02)

Fresh exports: Klaviyo US 2026-10-02T17:57Z (818,110 profiles), Shopify US 2026-10-02T18:02Z (649,535 customers, 508,539 with an email). The six pilot test accounts are excluded. **508,533** profiles match a Shopify customer by email; no email is on more than one Shopify customer. The per-customer plan is in `exports/consent_sync/dryrun/plan_20261002.csv`. **Recompute immediately before the run.**

| Klaviyo | Shopify now | Action | Count |
|---|---|---|---|
| subscribed | SUBSCRIBED | match | 143,791 |
| subscribed | NOT_SUBSCRIBED | write `SUBSCRIBED` | 72,759 |
| subscribed | UNSUBSCRIBED | write `SUBSCRIBED` | 321 |
| unsubscribed | UNSUBSCRIBED | match | 40,992 |
| unsubscribed | SUBSCRIBED | write `UNSUBSCRIBED` | 72,184 |
| unsubscribed | NOT_SUBSCRIBED | write `UNSUBSCRIBED` (D10) | 101,489 |
| suppressed | UNSUBSCRIBED | match | 522 |
| suppressed | SUBSCRIBED | write `UNSUBSCRIBED` (D3) | 7,477 |
| suppressed | NOT_SUBSCRIBED | write `UNSUBSCRIBED` (D3) | 6,787 |
| never | NOT_SUBSCRIBED | match | 62,177 |
| never | SUBSCRIBED | **Klaviyo → subscribed** with Shopify's date (D14) | 26 |
| never | UNSUBSCRIBED | leave; counts as a match (D12) | 2 |
| never | INVALID | can't be written | 6 |

**Shopify writes: 261,017** (73,080 to `SUBSCRIBED`, 187,937 to `UNSUBSCRIBED`). **Klaviyo writes: 26** (D14). In **506** writes Shopify's consent date is newer than or equal to Klaviyo's. They're overwritten (D11) and dated at the sync time (D16). No write has a future date or is missing a date. A client-facing summary of these counts is the doc "Shopify email consent sync – planned changes".

## 5. Consent dates and authority (for discussion)

- **What Klaviyo's date means:** for profiles the migration touched, Klaviyo's `consent_timestamp` is often the import time (Sep 26–27), not the customer's original action. The original is kept in `ca_consent_timestamp`. For US-only profiles it's the real date.
- **Which date to write to Shopify (Q6):**
  - (a) Klaviyo's `consent_timestamp`, so the two match field for field
  - (b) the original action date (`ca_consent_timestamp` where present)
  - (c) the sync time
- **Shopify newer than Klaviyo:** the user's position is to overwrite. One caveat: the integration brings Shopify checkout opt-ins into Klaviyo (method `SHOPIFY`, "Customer Webhook"), so a genuine newer Shopify opt-in should already be in Klaviyo. Where it isn't, that's a sync gap rather than stale data. Proposal: overwrite as decided, but have the dry run list "Shopify newer" cases separately so the volume is known before the run (Q5).
### 5.1 How many conflicts (D6)

Writes where Shopify's consent date is **newer** than Klaviyo's original date: **536** on Sep 28 data (0.3% of writes); **504** in the 2026-10-02 dry run. Per D11, all are overwritten.

| Klaviyo → write | Shopify now | Count | What overwriting means |
|---|---|---|---|
| subscribed → SUBSCRIBED | NOT_SUBSCRIBED | 185 | low risk |
| subscribed → SUBSCRIBED | UNSUBSCRIBED | **165** | **re-subscribes people whose latest action on record is a Shopify unsubscribe**: the riskiest group |
| suppressed → UNSUBSCRIBED | SUBSCRIBED | 181 | removes a newer Shopify opt-in from a profile Klaviyo can't email anyway |
| unsubscribed → UNSUBSCRIBED | SUBSCRIBED | 5 | removes a newer Shopify opt-in |

The 26 "never → NOT_SUBSCRIBED" writes have no Klaviyo date. They'd take the sync time.

### 5.2 Feedback loop

- **Feedback loop:** a Shopify consent write fires Shopify's customer webhook, and Klaviyo ingests consent from it. The pilot must show whether writing Shopify changes Klaviyo back, for example a new consent date or a re-subscribe with method `SHOPIFY`. If it does, the run would rewrite Klaviyo's consent history.

### 5.3 Welcome Series risk (D9)

The live flows **Welcome Series - APRIL 2024** (Xh2aBA) and **… - Ground** (XpJLCe) trigger on *Added to List → LOF USA Newsletter - Main* (Xz4KGg). Their only profile filter is "Placed Order = 0". The Shopify→Klaviyo subscriber sync adds Shopify subscribers to that list, so writing `SUBSCRIBED` for ~73.6k customers could enrol every one of them not already on the list, many of them migrated CA customers with no US orders.

Mitigation (preferred): point "Sync Shopify email subscribers to Klaviyo" at a dummy list with no flows for the duration of the run, then restore it. Afterwards, list the dummy list's members, drop everyone in the sync file, and add the rest (real checkout signups during the window) to Main. That gives them their Welcome then. Alternative: a temporary profile filter on the two Welcome flows. The pilot confirms whether an API consent write triggers the list add at all.

### 5.4 Run checklist (settings to change and restore)

Before the run:

1. Full Klaviyo US backup (D8) and a fresh Shopify customer export.
2. Point "Sync Shopify email subscribers to Klaviyo" at the dummy list (D9).
3. Turn **off** "Sync Klaviyo profiles to Shopify" (D13).

After the run and validation:

4. Turn "Sync Klaviyo profiles to Shopify" back **on** (D13).
5. Point "Sync Shopify email subscribers to Klaviyo" back at **LOF USA Newsletter - Main** (D9).
6. Move genuine signups from the dummy list to Main (D9).

## 6. Validation after the sync

1. Fresh Klaviyo US export and Shopify bulk export (both commands exist and are read-only on Shopify).
2. A comparison by email: Klaviyo consent against Shopify `marketingState`, plus the date where we wrote one. It produces counts and a per-customer mismatch CSV, like the Sep 28 three-way comparison.
3. Expected result: zero mismatches in the synced set, except listed, accepted exceptions (for example `REDACTED` or `INVALID` customers, or Shopify refusals).
4. A before/after comparison of Klaviyo itself, to prove the sync didn't change Klaviyo consent (the feedback loop in section 5).
5. Spot checks in the Shopify admin and in Attentive for the pilot accounts.

## 7. Proposed plan

1. **Agree** on the open questions below.
2. **Back up** Klaviyo US in full (D8, `migtool klaviyo profiles export --instance klaviyo_us`, about 1.5 hours), plus a fresh Shopify customer export, immediately before the run.
3. **Refresh the data:** run the CA post-cut-off sweep first (task #1), so Klaviyo is final. Then take fresh exports and dry-run the diff, with counts by transition and a "Shopify newer" list. Read-only.
4. **Pilot with test accounts** (and Attentive's UI). For each case, record what Shopify, Klaviyo and Attentive show afterwards. Cases:
   - subscribed → unsubscribed
   - not subscribed → subscribed
   - unsubscribed → subscribed
   - a backdated consent date
   - subscribed → not subscribed (D2), which may not be allowed; the fallback is `UNSUBSCRIBED`
   - and for each: whether Klaviyo's consent, consent date or method changes back, whether the profile joins LOF USA Newsletter - Main, and what Attentive shows
5. **Build** `shopify consent-sync` on a branch with a PR: dry run by default, typed confirmation, `--limit`, resumable, per-row results, and only the consent mutation.
6. **Pilot on real data** with a small `--limit` and the validation steps.
7. **Full run**, then validation (section 6), with a record of the run.

## 8. Open questions

- ~~Q1. Scope~~ → D1. ~~Q2. SMS~~ → out of scope (D1).
- ~~Q3. Mapping~~ → D2, D3; remaining cases are Q12 and Q13.
- ~~Q4. Token~~ → D4.
- ~~Q5. Conflicts~~ → D11.
- ~~Q6. Dates~~ → D5.
- **Q7. Opt-in level:** `SINGLE_OPT_IN` for everyone, or `CONFIRMED_OPT_IN` where Klaviyo has `double_optin`?
- ~~Q8. Integration settings~~ → D7.
- **Q9. Attentive:** which test accounts, and who checks Attentive's UI during the pilot?
- **Q10. Matching:**
  - Email only: the 130k phone-only Shopify customers would be out of scope for email consent.
  - What about duplicate emails, or customers whose Shopify email differs from Klaviyo's? Example: the order-email mismatches found on 2026-09-29.
- **Q11. Timing:** a quiet window? The run will fire Shopify customer webhooks to every connected app.
- ~~Q12~~ → D10.
- ~~Q13~~ → D12.
- ~~Q14~~ → D13.
- ~~Q15~~ → D15.

## 9. Test-account pilot (draft for approval)

**Purpose:** answer, on test accounts only, the questions that decide the build:

- what Shopify accepts
- what flows back into Klaviyo
- what Attentive does

Nothing here touches a real customer.

### 9.1 How the writes are made

- **One-off pilot script** in `exports/consent_sync/pilot/` (not part of migtool). It only sends `customerEmailMarketingConsentUpdate`, and only for customer IDs on a hard-coded **allowlist** of the test accounts. It refuses any other ID.
- Every step is a dry run first: it prints the mutation and the customer's current state, then sends only after the user approves it.
- The user reviews the script before its first run.
- The Shopify admin UI isn't used for the writes: it can't set a back-dated consent date, and it isn't the API path the real run will use.

### 9.2 Settings during the pilot

Run it in the **same configuration as the real run**, so what we see is what the run will do:

- "Sync Klaviyo profiles to Shopify" **off** (D13)
- "Sync Shopify email subscribers to Klaviyo" pointed at the **dummy list** (D9). A test account appearing on the dummy list means it would have joined LOF USA Newsletter - Main.

Restore both after the pilot, unless the run follows straight after.

### 9.3 Test accounts

Six `nick+consentpilot1..6@0xb8.net`-style accounts. Each must exist as a **Shopify US customer** and a **Klaviyo US profile**. At least two must also be in **Attentive**, one of them SMS-subscribed with a phone number. The user creates or chooses them. The starting states are set by the pilot script's "setup" step, which is also a consent write, on allowlisted accounts only.

### 9.4 Cases

Each case runs on its own account. Dates are fixed, back-dated values.

| Case | Real-run group (count, Sep 28) | Klaviyo state (set up first) | Shopify before → write | `consentUpdatedAt` sent | Questions |
|---|---|---|---|---|---|
| P1 | subscribed, Shopify not subscribed (73,275) | SUBSCRIBED | NOT_SUBSCRIBED → **SUBSCRIBED** | 2024-05-01 | Back-dated date kept? Klaviyo consent date or method changed? Joins the dummy list? Welcome email? |
| P2 | unsubscribed, Shopify subscribed (72,151) | UNSUBSCRIBED | SUBSCRIBED → **UNSUBSCRIBED** | 2025-03-01 | Klaviyo's unsubscribe date rewritten? Any event? |
| P3 | unsubscribed, Shopify not subscribed (101,685; D10) | UNSUBSCRIBED | NOT_SUBSCRIBED → **UNSUBSCRIBED** | 2025-03-01 | Allowed? Webhook effects on Klaviyo? |
| P4 | never, Shopify subscribed (26; D2) | NEVER_SUBSCRIBED | SUBSCRIBED → **NOT_SUBSCRIBED** | (none) | **Allowed at all?** If refused, the error text, and whether UNSUBSCRIBED is the fallback |
| P5 | never, Shopify unsubscribed (2; D12) | NEVER_SUBSCRIBED | UNSUBSCRIBED → **NOT_SUBSCRIBED** | (none) | Allowed? |
| P6 | subscribed, Shopify unsubscribed (318) | SUBSCRIBED | UNSUBSCRIBED → **SUBSCRIBED** | 2026-06-01 | Re-subscribe allowed with a date? Klaviyo effects |
| P7 | re-run safety | (P1's account) | SUBSCRIBED → **SUBSCRIBED** again | same | No-op or error? Does a webhook still fire? (decides resume behaviour) |
| P8 | date edge | (P2's account) | UNSUBSCRIBED → **UNSUBSCRIBED** | a future date, and a date older than the current one | Rejected, clamped or accepted? |
| P9 | SMS untouched | an Attentive SMS subscriber | email change as in P2 | — | Shopify `smsMarketingConsent` unchanged? Attentive SMS and email status unchanged? |

### 9.5 What's recorded after each write

At **+1, +5 and +30 minutes**, in `exports/consent_sync/pilot/results.csv`:

- **Shopify:** the customer's `emailMarketingConsent` (state, opt-in level, `consentUpdatedAt`), `smsMarketingConsent` and `updatedAt`. Read-only query.
- **Klaviyo:** the profile's email consent, consent date, `method` and `method_detail`, suppressions, whether it's on the dummy list, Main or LOF Canada Newsletter, plus new events since the write (Subscribed/Unsubscribed to Email Marketing, Added to List, Received Email). Read-only API.
- **Attentive (user, in the UI):** the contact's email and SMS subscription status, and anything new in their timeline.

### 9.6 Pass criteria

1. Shopify accepts P1–P3 and P6 with the dates as sent (or a known, consistent adjustment).
2. Klaviyo's consent state, date and method don't change because of the Shopify write. If they do, the run would rewrite Klaviyo history, and we stop and rethink.
3. No test account receives a Welcome or other flow email, and none joins a list other than the dummy.
4. Shopify SMS consent and Attentive are unchanged (P9).
5. P4/P5 settle D2/D12 (allowed, or fall back to UNSUBSCRIBED / leave).
6. P7/P8 tell us whether a re-run is safe and how dates are validated.

### 9.7 Afterwards

Restore the test accounts to their original states if the user wants, then write up the results here. Those results feed the build spec (task #9).

### 9.8 Results (run 2026-10-02 19:35–20:24 UTC)

The six test accounts are recorded in `exports/consent_sync/pilot/results.csv`. Settings during the pilot: "Sync Klaviyo profiles to Shopify" off; "Sync Shopify email subscribers to Klaviyo" on, into NK_consentsync (VhVV8j).

| Case | Write | Result |
|---|---|---|
| setup | 2× → `SUBSCRIBED` 2025-01-15; 2× → `UNSUBSCRIBED` 2025-02-15 | Accepted; dates kept as sent |
| P1 (pilot4) | `NOT_SUBSCRIBED` → `SUBSCRIBED`, 2024-05-01 | Accepted; date kept |
| P2 (pilot3) | `SUBSCRIBED` → `UNSUBSCRIBED`, 2025-03-01 | Accepted; date kept |
| P3 (pilot1) | `NOT_SUBSCRIBED` → `UNSUBSCRIBED`, 2025-03-01 | Accepted; date kept |
| P4 (pilot2) | `SUBSCRIBED` → `NOT_SUBSCRIBED` | **Refused**: "Cannot specify NOT_SUBSCRIBED as a marketing state input"; nothing changed |
| P5 (pilot5) | `UNSUBSCRIBED` → `NOT_SUBSCRIBED` | **Refused** (same) |
| P6 (pilot6) | `UNSUBSCRIBED` → `SUBSCRIBED`, 2026-06-01 | Accepted; date kept |
| P7 (pilot4) | identical `SUBSCRIBED` write again | Accepted; consent unchanged, but the customer's `updatedAt` changed, so a customer-update notification fires |
| P8a (pilot1) | `UNSUBSCRIBED` with a future date (2027-01-01) | **Refused**: "Consent updated at must not be in the future"; nothing changed |
| P8b (pilot1) | `UNSUBSCRIBED` (state unchanged) with an older date (2024-01-01) | Accepted with no error, but **the date wasn't changed** and `updatedAt` didn't move |
| P9 (pilot3) | email changes on an Attentive SMS subscriber | Shopify SMS unchanged; Attentive SMS still subscribed |

Side effects, checked at +1, +5 and +30–47 minutes:

- **Klaviyo: none.** No consent, date or method changes and no events from any Shopify write; nobody added to NK_consentsync or LOF USA Newsletter - Main; no emails. The pilot showed the Welcome Series risk (5.3) doesn't materialise for API writes. The dummy list stays as insurance (D9), together with a canary batch.
- **Klaviyo → Shopify with the sync off: none.** Klaviyo unsubscribes made during setup didn't reach Shopify.
- **Shopify SMS consent: unchanged** on every account.
- **Attentive (user, UI): unchanged.** Attentive shows no email consent for pilot3 or pilot4 and pilot3's SMS subscription stayed. Separately, pilot3 being SMS-subscribed in Attentive didn't make Shopify's SMS state subscribed.
- One real customer signed up during the pilot window and landed on NK_consentsync; they were moved to Main afterwards (task #12).

Consequences for the build (phase 6):

1. `NOT_SUBSCRIBED` is never sent: D2 becomes D14, and D12 is left as is.
2. Read each customer's current state just before writing, and **skip customers already in the target state**. An identical write still counts as a customer update.
3. **Never send a future date.** A date can only be set together with a state change, so validation compares dates only for customers whose state the run changed.
4. Only `emailMarketingConsent` is written; SMS isn't touched.
5. Klaviyo's back-dated subscribes took 10–20 minutes to apply on 2026-10-02. D14 runs first and is checked before the Shopify writes start.
