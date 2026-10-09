# Cara requirements, inspection checklist and backlog

Inspection baseline: 2026-10-08; existing deployed voice/booking branch `codex/cara-grok-ara`, commit `a461fd8b9f3a783c76fa25130cfa03bedf9f2934`, plus the local undeployed manager increment. Code presence, automated verification and production evidence are distinguished below. No live campaigns are authorized during development.

## 1. Already working

- [x] Twilio phone connection and Grok Ara realtime audio for inbound and outbound paths. Preserve existing media/audio settings. Production voice reported working; automated voice tests pass.
- [x] Square booking creation with duration-specific service resolution, authoritative availability, exact selected slot/staff recheck, timezone handling and provider-success confirmation. Production logs show a successful provider create and local persistence after the booking fix.
- [x] Supported Square secure card-on-file link, tokenized collection and off-call completion handling; raw payment details are not collected by the voice booking tools. Provider create/card tests pass; live completion of the customer's card form has not been observed.
- [x] Tenant-scoped business information, hours/timezone, service CRUD, staff service qualifications, receptionist instructions, configured packages/cancellation/card policies and spa facts tools.
- [x] Tenant-scoped customers, appointments and credentials; role checks; encrypted booking configuration, masked credentials and signature validation.
- [x] Local manager test flow: evidence-based 90-day proposal, immutable approval, test outbox, YES/STOP response, simulated booking, local offers and results/audit. Undeployed, not a live campaign integration.

## 2. Present but needs repair or completion

- [x] Repaired locally: establishment masters are explicitly assigned spa administrators, separate from platform administrators. Approval is bound to that user's establishment and current authority. Assignment/revocation is audited; platform admins cannot approve a business campaign.
- [x] Continued locally: the development-only campaign adapter now uses the existing staging/read-back/confirmation/recheck/persistence pipeline. Test appointments are persisted with provider `test_pipeline`. External sandbox booking and real payment collection remain unverified and disabled for campaigns.
- [x] Continued locally: request classification distinguishes questions, proposed preferences, campaign proposals, unsupported changes, action requests and clarification. Weekday discount restrictions and exact-service promotion with expiry can be reviewed and confirmed; a request does not silently execute.
- [ ] Reschedule/cancel tools and Square write-back exist; run focused checks and verify connected-provider behavior before describing every provider as supported.
- [x] Repaired locally: removed universal 24-hour card-policy statements from both shared voice copy and spa-fact lookup. Configured business policy is returned separately; charging fees/deposits is still unimplemented. No global $39 rule.
- [ ] Staff service qualifications and Square provider preference/availability are enforced. Dedicated staff schedules, preference history and complete owner management remain incomplete.
- [ ] Configured staff notifications exist. Customer confirmations/reminder scheduling and delivery tracking need completion.
- [ ] Existing outbound phone/sales-calendar paths need owner-approved audience/script/consent/schedule gating and auditable task execution; a phone dial endpoint is not a campaign approval system.
- [ ] Outbound Ara is wired to the platform sales workspace. Establishment appointment outreach by phone needs a separate tenant-scoped objective/routing path; do not send spa appointments to the sales calendar.
- [ ] Inbound answering is not gated by business hours; booking checks configured opening hours. Dedicated after-hours callback/escalation rules and their complete call behavior need verification and owner controls.
- [ ] Global xAI voice choice remains Ara. Per-business supported voice choice UI/settings and validation need completion without changing current voice defaults.
- [ ] Frontend TypeScript passes; standard production bundling was blocked by local Windows esbuild execution permissions. Bundle/browser QA remains required before release.
- [ ] Manager migration generated PostgreSQL SQL, but is unapplied. PostgreSQL concurrency and release checks remain required.

## 3. Missing project backlog

- [ ] Waitlist persistence, preferences, cancellation-triggered suitable openings, consent/approval rules, hold expiry and duplicate prevention.
- [ ] Dedicated promotions/packages editor, multi-service personalized and holiday bundles, live capacity-aware proposals and redemption tracking.
- [ ] Rich owner conversation for Thursday openings, monthly facial promotion, Saturday discount prohibitions and holiday proposals. Explicit clarification, scope, review and durable confirmed preferences.
- [ ] Fine-grained staff permissions and business master assignment/revocation UI with audit.
- [ ] Customer reminders/confirmations through configured channels, delivery receipts, opt-outs, scheduler/retry/cooldown behavior.
- [ ] Per-establishment approved messaging sender/channel mapping, inbound reply routing and opt-out isolation; do not assume the shared SMS helper is a completed tenant campaign sender.
- [ ] Approved appointment outreach and sales-presentation scheduling with exact audiences, scripts, channels and schedules, plus safe customer reply orchestration.
- [ ] External appointment-history import with completion/provider provenance, confirmed customer preferences and complete engagement history.
- [ ] Verified revenue, gift-card balances/redemptions and membership entitlements; never substitute estimates for verified data.
- [x] Local request flag added for owner instructions and call-session health-related terms, persisted in realtime call analysis and visible for human review. Medical-office workflows remain disabled pending technical/security/contractual review. This heuristic is not complete PHI detection and makes no HIPAA-compliance claim.
- [ ] Complete dashboard controls for deposit rules, cancellation windows/fees, staff schedules, waitlist, reminders, outreach and per-establishment voice settings.

## 4. Blocked by integration or setup

| System | Implemented authority | Unsupported/unverified | Required next step |
|---|---|---|---|
| Square | Connected business's external bookings, live staff/service availability, customer directory and saved-card status; local appointments are dashboard records | Gift-card/member/history import, external packages, deposits/fee charges, verified revenue; campaign sandbox orchestration | Compatible sandbox application/token/location and bookable catalog/staff for safe external testing; inspect API/scopes before new actions |
| Google Calendar | Calendar scheduling through existing adapter, including sales presentation routing | Customer financial/membership data and payments | Valid tenant/workspace OAuth/calendar configuration; verify adapter operations |
| MangoMint | Provider selector/configuration scaffold only (`implemented=False`) | Availability, writes, customers, balances and memberships are not implemented | Supported API/access agreement and real adapter; never claim MangoMint accepted a booking |
| Mindbody / Vagaro / Zenoti | Configuration scaffolds (`implemented=False`) | Live provider capabilities are not implemented | API credentials/contracts plus supported adapters and tests |
| GoHighLevel | No connector/adapter found in backend or dashboard | All proposed CRM/outreach/bookings/balance capabilities | Decide authoritative CRM and connect supported location-scoped API; inspect permissions first |
| Twilio | Phone transport, current telephony webhooks and secure-card/staff SMS support | Approved campaign sender, inbound marketing replies, scheduled reminder delivery tracking | Tenant sender/channel configuration, consent data and tested approval-gated execution |
| xAI | Ara realtime voice, existing Grok conversational tools | Independent per-business voice choices and broader owner task reasoning | Validate supported choices/settings; preserve existing Ara default |

Presence in a provider dropdown does not mean an integration is connected or implemented. Show actual configured authority and readiness without revealing credentials. Do not fall back from an unavailable external system and claim that external system booked an appointment.

## Current implementation priority

1. Preserve and regression-test working voice/booking/card features.
2. Correct establishment-master approval boundaries and manager request classification.
3. Continue the reactivation flow through a supported booking adapter in test mode; no real outreach, production calendar writes, charges or medical workflows during development.
4. Persist this entire backlog; implement remaining requirements incrementally after the first safe flow is verified.

## Follow-on verification and release status

365 regression checks passed, including existing voice, booking/card behavior, role/tenant boundaries and the new manager/pipeline flow. TypeScript checks passed. Vite bundling remains blocked by this Windows environment refusing to start esbuild (`spawn EPERM`); rendered browser QA remains pending. Migration head is `a102_business_master`, adding establishment-master authority and persisted test booking sessions. Incremental PostgreSQL SQL generated successfully; no live migration, deployment, outreach, charge or production calendar write was performed.

The dashboard also offers actual recorded customer data for review-only proposals. External history, balances and membership entitlements remain unknown when not supplied by implemented adapters. Test fixtures and the shared-pipeline test are forbidden on a production backend.

Square now has a validated per-establishment production/sandbox setting in the existing configuration editor. The working business's production setting and Ara voice were not changed. For future external tests, use a separate development tenant with Square sandbox access token, location ID, application ID for Web Payments, bookable service variations and qualified/bookable staff. Do not substitute sandbox credentials into the live business.

Square documents that its Bookings API cannot book a service with a nonzero `no_show_fee`. This is an integration limitation to verify before implementing an establishment's fee/deposit policy, not permission to remove or change that policy. [Square booking requirements](https://developer.squareup.com/docs/bookings-api/use-the-api).

HighLevel exposes appointment endpoints with location-scoped authorization, but this application has no GoHighLevel implementation or verified connection. [HighLevel calendar endpoints](https://marketplace.gohighlevel.com/docs/ghl/calendars/calendar-events/), [authorization scopes](https://marketplace.gohighlevel.com/docs/Authorization/Scopes/index.html). MangoMint's product integrations do not establish that this application's stub can perform booking operations; supported API access must be obtained and verified first. [MangoMint integration management](https://www.mangomint.com/learn/managing-your-apps-and-integrations/).

Final focused verification: all 29 manager checks passed, including platform-only master assignment, production fixture blocking and rejection of unqualified staff.

Definition of the eventual complete integration: approved offer/audience/message/schedule -> consenting customer reply -> provider-confirmed exact availability -> explicit customer selection -> authoritative booking success -> secure card/deposit step if supported/required -> durable results/audit. A simulator is development evidence, not proof of a connected production campaign flow.


## Smart Enhancements review build — October 9, 2026

Implemented locally, disabled by default: tenant controls, deterministic Square variant upgrades, exact availability and fixed-price verification, explicit additional-cost consent, original booking recovery, cached owner-reviewed Grok invitation wording, privacy/retention, and provider-backed reporting. Ara and existing booking guards preserved. No deployment approved or performed.

Remaining integration backlog: atomic multi-service/multi-provider booking, independently verified room/resource allocation, other booking providers, supported discount/package write APIs, payment/cancellation/refund reconciliation, provider-wide popularity evidence, and additional currency minor-unit formats. Provider-defined fixed-price single-service catalog bundles are the supported alternative.

Release prerequisites: reconcile unreleased manager migration dependencies before a scoped rollout; staging PostgreSQL migration and real provider readback; successful Vite bundle and browser validation (Windows esbuild spawn EPERM remains); authorized test call after approval. Full suite: 688 passed with the same 13 failures reproduced on unchanged aa458ce voice code; 38 enhancement tests pass. See Cara-smart-enhancements-implementation.md in the workspace root.
