# Klaviyo US → Shopify US consent sync (plan, not started)

Status: **discussion**. No code, no integration changes, and no writes to Klaviyo, Shopify or Attentive until the open questions below are answered and the plan is agreed.

## 0. Decisions so far (2026-10-02)

| # | Decision |
|---|---|
| D1 | **Email marketing only.** No SMS consent changes are made (Klaviyo has no SMS consent state; see 2.3). The client is told this. |
| D2 | Klaviyo **never subscribed** → Shopify **`NOT_SUBSCRIBED`** ("null"). Shopify `SUBSCRIBED` is changed to `NOT_SUBSCRIBED`; Shopify `NOT_SUBSCRIBED` counts as a match. |
| D3 | Klaviyo **suppressed** (bounce, spam complaint, manual) → Shopify **`UNSUBSCRIBED`**, whatever Shopify shows now. |
| D4 | A separate Shopify token with `write_customers` is provisioned by the user for this job; the current token stays read-only. |
| D5 | Shopify's consent date is set to the **original** consent date: `ca_consent_timestamp` / `ca_suppression_timestamp` for migrated CA profiles, Klaviyo's own date otherwise. |
| D6 | **Shopify-newer conflicts:** count them before deciding (section 5.1). Nothing changes until there's a decision. |
| D7 | Klaviyo app settings in Shopify US (read by the user): *From Shopify*: "Sync Shopify email subscribers to Klaviyo" **on**, into list **LOF USA Newsletter - Main** (Xz4KGg); SMS sync on but inactive (texting not set up). *To Shopify*: "Sync Klaviyo profiles to Shopify" **on**, existing Shopify customers only; it creates no new customers. |
| D8 | **Full Klaviyo US backup** (all ~815k profiles) immediately before any change. |
| D9 | **Welcome Series protection:** point the Shopify→Klaviyo subscriber sync at a dummy list (no flows) for the duration of the run (preferred), or filter the two Welcome flows. See 5.3. |
| D10 | Klaviyo **unsubscribed**, Shopify `NOT_SUBSCRIBED` → write **`UNSUBSCRIBED`** (Q12). |
| D11 | **Overwrite all** Shopify-newer conflicts, including the 165 re-subscribes (Q5): Klaviyo is the source of truth. |
| D12 | Klaviyo **never subscribed**, Shopify `UNSUBSCRIBED` → `NOT_SUBSCRIBED` if Shopify allows it, otherwise leave `UNSUBSCRIBED` (Q13). |
| D13 | The user turns **"Sync Klaviyo profiles to Shopify" off** for the duration of the run and back on afterwards; both steps are on the run checklist (Q14). |


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

So the matched set is about 508k (not ~300k). Under the agreed mapping (section 4.1), **261,490** need a write; the rest already match. That count is higher than the simple subscribed/not-subscribed split above because suppressed profiles and never-subscribed cases are treated separately.

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

## 4.1 Mapping and counts (Sep 28 data; refresh before the run)

Matched profiles (Klaviyo email = Shopify email): 508,065. The target state comes from D1–D3. Klaviyo "unsubscribed" includes profiles whose only suppression is `UNSUBSCRIBE`.

| Klaviyo | Shopify now | Action | Count |
|---|---|---|---|
| subscribed | SUBSCRIBED | match | 143,705 |
| subscribed | NOT_SUBSCRIBED | write `SUBSCRIBED` | 73,275 |
| subscribed | UNSUBSCRIBED | write `SUBSCRIBED` | 318 |
| unsubscribed | UNSUBSCRIBED | match | 40,116 |
| unsubscribed | SUBSCRIBED | write `UNSUBSCRIBED` | 72,151 |
| unsubscribed | NOT_SUBSCRIBED | write `UNSUBSCRIBED` (D10) | 101,685 |
| suppressed | UNSUBSCRIBED | match | 513 |
| suppressed | SUBSCRIBED | write `UNSUBSCRIBED` | 7,273 |
| suppressed | NOT_SUBSCRIBED | write `UNSUBSCRIBED` (D3) | 6,760 |
| never | NOT_SUBSCRIBED | match (D2) | 62,235 |
| never | SUBSCRIBED | write `NOT_SUBSCRIBED` (D2) | 26 |
| never | UNSUBSCRIBED | write `NOT_SUBSCRIBED` if allowed, else leave (D12) | 2 |
| never | INVALID | can't be written | 6 |

**Writes: 261,490** (73,593 to subscribed; 187,869 to unsubscribed; 28 to not subscribed).

## 5. Consent dates and authority (for discussion)

- **What Klaviyo's date means:** for profiles the migration touched, Klaviyo's `consent_timestamp` is often the import time (Sep 26–27), not the customer's original action. The original is kept in `ca_consent_timestamp`. For US-only profiles it's the real date.
- **Which date to write to Shopify (Q6):**
  - (a) Klaviyo's `consent_timestamp`, so the two match field for field
  - (b) the original action date (`ca_consent_timestamp` where present)
  - (c) the sync time
- **Shopify newer than Klaviyo:** the user's position is to overwrite. One caveat: the integration brings Shopify checkout opt-ins into Klaviyo (method `SHOPIFY`, "Customer Webhook"), so a genuine newer Shopify opt-in should already be in Klaviyo. Where it isn't, that's a sync gap rather than stale data. Proposal: overwrite as decided, but have the dry run list "Shopify newer" cases separately so the volume is known before the run (Q5).
### 5.1 How many conflicts (D6)

Writes where Shopify's consent date is **newer** than Klaviyo's original date: **536** (0.3% of writes).

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
