# Cara staging release review — October 9, 2026

Repository: `6DM-hub/6dm-Appointment-Setter-Spa-Receptionist`. Production branch: `codex/cara-grok-ara`, baseline `aa458ce53efb477029cd6790c57ea1a92b8a8724`. Prepare these changes on a separate staging branch; never move the production branch before release approval.

## Included

Preserve the deployed voice, greeting, consent, exact-slot grounding, recovery and outbound realtime paths. Add the pending owner assistant, establishment-master approval, test reactivation pipeline, tenant preferences/audit, sensitive-request flag, business-specific card wording, and optional verified Square enhancement engine/dashboard. Enhancements are disabled by default. No voice ID or transport settings change.

Changes were compared directly with the production commit. Unmodified files stay on the production tree. This release includes migrations `a101_cara_manager`, `a102_business_master`, and `a103_smart_enhancements`, in order, after `d1e2f3a4b5c6`.

## Evidence and limits

- Complete backend suite before the additional environment guard: 688 passed, 13 failed. The same 13 failures reproduce with unchanged production realtime code. Fixed past-date fixtures and outdated expectations for repeated speech/probe continuations need separate maintenance; do not weaken current protections to satisfy those assertions. The additional six production-environment guard cases and the manager/enhancement suite pass together (73 passed); final full-suite results are recorded in the workspace release report.
- Manager tests exercise approval, outbox, customer response, slot selection, explicit consent, final recheck, durable appointment save and results through the shared booking pipeline and HTTP API. The adapter is `test_pipeline`, not Square.
- Enhancement tests verify prices/durations/provider/slot, explicit extra-cost approval, original recovery, opt-outs, tenant isolation, idempotency and reporting. Provider boundaries are mocked.
- Production startup evidence for baseline aa458ce: voice `ara`, catalogue `ara_verified`, actual inbound/outbound xAI audio probes ready, PCMU at 8000 Hz, Redis connected, Twilio number routing matched. This is connection evidence, not an audible phone-call test of this pending release.
- TypeScript checking passes. Standard Vite bundling is blocked locally by esbuild `spawn EPERM`; Linux build and browser QA remain release gates.
- Alembic has one head. Offline PostgreSQL migration generation is checked; application against an isolated PostgreSQL database remains unverified.
- No staging deployment, real Square readback, secure-card completion or live voice test has passed for this combined release yet. Production stays unchanged.

## Isolated staging setup

Railway project `AI Scheduler` has only production as inspected. Create an EMPTY `staging` environment in that project, or connect an existing isolated staging project. Do not clone production customer data, credentials, Redis, database or phone routing.

Provision staging Postgres and Redis, backend from the staging branch with root `/backend`, and frontend with root `/frontend`. Backend start command: `sh -c "python -m alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"`. Health path `/api/v1/health`. Build frontend with the normal package build script.

Set backend variables in the STAGING service only:

| Variable | Required staging value |
|---|---|
| `APP_ENV` | `staging` |
| `DEBUG` | `false` |
| `DATABASE_URL` | Reference staging Postgres only |
| `REDIS_URL` | Reference staging Redis only |
| `SECRET_KEY` | New random secret of at least 32 bytes; enter through Railway, never chat/logs |
| `PUBLIC_BASE_URL` | Staging backend HTTPS URL |
| `FRONTEND_BASE_URL`, `CORS_ORIGINS` | Staging frontend HTTPS URL |
| `VOICE_SETUP_ON_STARTUP` | `false` until isolated test phone credentials/routing are verified; startup setup rewrites Twilio number routing |
| `TWILIO_SYNC_ENABLED` | `false` to avoid importing production call history |
| `TWILIO_VALIDATE_SIGNATURE` | `true` for externally reachable Twilio webhooks |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Separate authorized test account credentials for phone testing |
| `TWILIO_PHONE_NUMBER` | Dedicated test number only |
| `XAI_API_KEY` | Authorized xAI realtime key; enter through Railway, never chat/logs |
| `XAI_VOICE_ID` | `ara` in staging; do not alter existing production value |
| `XAI_REALTIME_URL` | `wss://api.x.ai/v1/realtime` |
| `XAI_REALTIME_ENABLED` | `true` for authorized staging outbound realtime tests |
| `VOICE_RELEASE` | Exact staging candidate commit |

Frontend: set `VITE_API_BASE_URL` to the staging backend. Create a test establishment with `voice_engine=xai_realtime`, its actual test business name/timezone, a sandbox Square connection, qualified test staff, and actual fixed-price sandbox catalog variations. Square sandbox credentials/location/application ID belong in that establishment's booking settings. Set its Square environment to `sandbox`; never change the working production establishment to sandbox. Existing provider entitlements may limit Square sandbox booking operations; report any such rejection before using a different test method.

The Railway connector redacts secret values and cannot create environments. Browser project access currently needs the user to sign in. No new integration connection or deployment may be claimed successful until verified. Release review also repaired the test-mutation guard: Railway's production environment name now blocks fixtures and all simulator booking/reply/outbox mutations even when APP_ENV is omitted. Explicit APP_ENV=production remains required configuration.

## Staging acceptance gates

1. Successful backend/frontend build, migrations and health checks on isolated resources; no unexpected startup errors.
2. Owner/test-master review -> immutable proposal approval -> test outbox -> YES -> live slot proposal -> clear acceptance -> final recheck -> provider success -> saved appointment/readback -> results; no real customer outreach.
3. Authorized test phone call: one exact-business greeting, audible CARE-uh, Ara, interruption and pauses, no duplicate acceptance questions or internal instructions. Silence cannot consent. At least three seconds before a gentle follow-up if a confirmation is still required. Accepted slots survive name collection.
4. Card explanation only after explicit appointment/upgrade acceptance; playback complete before create; supported secure link completes off-call with pending/completed state distinguished.
5. Accept/decline enhancement and extra cost; unavailable upgrade recovery; exact duration/provider/timezone; no duplicate writes or invented slots. Validate actual Square record, not only application logs.
6. Authorized outbound TEST call uses Grok realtime and dedicated test number; callback permission creates a request only after clear yes. Staff notification destination must be configured and delivered before claiming staff were notified.

## Remaining backlog and integration blocks

Live campaign sender/scheduler and inbound campaign reply orchestration are not implemented; test outbox is deliberate. Waitlist matching/notifications, provider reminder scheduling, broader holiday/capacity planning and per-business voice selection UI remain backlog. External gift-card/membership readback, MangoMint/GHL booking capabilities, external package/discount writes, atomic multi-service/provider/resource upgrades and verified realized revenue need supported integrations. Medical workflows remain disabled; no HIPAA claim.

## Production approval, only after staging passes

Present exact candidate commit, staging evidence and migration plan. Obtain approval to move/deploy that commit to `codex/cara-grok-ara` on production backend service `0e40cec6-fa4d-459d-ba84-48f4676850b9`, production environment `db6835e4-8ffe-4548-ac00-b286ca248c8b`; deploy matching dashboard; back up production PostgreSQL and apply the three migrations; explicitly set `APP_ENV=production`; assign the correct establishment master via the audited platform operation. Check the existing signing/encryption secret without printing or blindly rotating it. Preserve Ara and current Twilio routing/credentials. Keep enhancements off until mappings are reviewed. No live campaign approval is included. Authorize a controlled live test separately and inspect the resulting provider record.
