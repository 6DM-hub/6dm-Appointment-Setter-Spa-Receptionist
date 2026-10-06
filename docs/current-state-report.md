# 6DM AI Caller: Current-State Report

Date: 2026-09-19
Scope: read-only review of the working tree at commit `bc2a4ff7` plus uncommitted changes. No production code was modified.

---

## 1. Executive summary

The system is a FastAPI + React platform that answers inbound spa calls and places outbound B2B sales calls using Twilio for telephony and xAI Grok for the conversational brain. Two voice pipelines coexist per tenant: a legacy Twilio `<Gather>`/`<Say>` text loop and a newer bidirectional Twilio Media Stream bridged to xAI's realtime speech-to-speech socket. Bookings flow through a draft/confirm state machine with an idempotency key, then out to Google Calendar or Square. Multi-tenancy is enforced in code and by database check constraints, and the backend has a real test suite that passes today.

Headline findings:

- **Working end to end**: inbound spa call routing by dialed number, both voice engines, per-tenant prompt assembly, availability check, draft-then-confirm booking, Google Calendar OAuth write-back, Square bookings, transcript and AI summary persistence, RBAC dashboard with tenant impersonation, browser test calls.
- **Most urgent risk**: the root `.env` with live Twilio, xAI, Google and Square secrets is committed to git. There is no root `.gitignore`. The `.venv` (9,784 files) and `frontend/node_modules` (8,178 files) are also tracked.
- **Second risk**: `SECRET_KEY` has no production guard, and the same key signs JWTs and derives the Fernet key protecting every tenant's booking credentials and Google tokens. Rotating it silently wipes all stored credentials.
- **Outbound sales bookings never reach Google Calendar** today: the sales adapter is constructed without an OAuth connection, so it degrades to local-only.
- **Large uncommitted work**: ~3,500 lines across 30 files plus 20 untracked files (four migrations, Square adapter, services API, booking state machine) are not committed.

---

## 2. Component inventory

| Concern | Current choice | Evidence |
|---|---|---|
| Phone provider | Twilio (programmable voice webhooks, Media Streams, Voice JS SDK for browser tests, optional Elastic SIP trunk to xAI) | [telephony.py](../backend/app/api/v1/telephony.py), [twilio_service.py](../backend/app/services/twilio_service.py) |
| Voice provider, engine A | Twilio ASR via `<Gather input="speech">` and Twilio/Polly TTS via `<Say>`; per-tenant `twiml_voice` | [telephony.py:170-195](../backend/app/api/v1/telephony.py#L170-L195) |
| Voice provider, engine B | xAI realtime speech-to-speech (`grok-voice-latest`, voice "Carina" in deployment) over G.711 μ-law, bridged from a Twilio `<Connect><Stream>` | [media_bridge.py](../backend/app/services/media_bridge.py), [xai_realtime.py:70](../backend/app/services/xai_realtime.py#L70) |
| Voice provider, engine C (dormant) | xAI-hosted SIP path: number trunked to `sip.voice.x.ai`, xAI POSTs `realtime.call.incoming`. Requires an xAI entitlement the team does not have. | [xai_voice.py](../backend/app/api/v1/xai_voice.py), [media_bridge.py:10-16](../backend/app/services/media_bridge.py#L10-L16) |
| Model provider | xAI chat completions, default `grok-4.20-0309-non-reasoning` (env sets `XAI_MODEL`). Used for reply generation, intent extraction, post-call analysis. | [grok_service.py](../backend/app/services/grok_service.py), [config.py:191-197](../backend/app/core/config.py#L191-L197) |
| Calendar integration | Google Calendar via per-spa OAuth (PKCE, offline refresh, Fernet-encrypted tokens); Square Bookings API (real); Mindbody, Mangomint, Vagaro, Zenoti (stubs, `implemented = False`) | [google_calendar.py](../backend/app/services/google_calendar.py), [square.py](../backend/app/services/booking_adapters/providers/square.py), [spa_router.py](../backend/app/services/booking_adapters/spa_router.py) |
| Database | PostgreSQL 16, SQLAlchemy 2 async (asyncpg) + sync (psycopg2 for Alembic), 8 tables, 9 linear migrations, one head | [models/](../backend/app/models/), [migrations/versions/](../backend/migrations/versions/) |
| Cache / session store | Redis 7: live call state (`call:state:<sid>`, 1h TTL), active-call set, JWT refresh blocklist, OAuth state + PKCE verifier | [call_state.py](../backend/app/services/call_state.py), [token_blocklist.py](../backend/app/services/token_blocklist.py) |
| Deployment | Backend: Docker image on Railway, runs `alembic upgrade head` then uvicorn on `$PORT`. Frontend: Vercel SPA rewrite. Local: docker-compose (postgres, redis, backend, frontend dev server). | [Dockerfile](../backend/Dockerfile), [vercel.json](../frontend/vercel.json), [docker-compose.yml](../docker-compose.yml) |
| Frontend | React 18, Vite 5, TypeScript strict, Tailwind 3, React Router 6, axios, Twilio Voice SDK. No state library, no tests, no lint config. | [package.json](../frontend/package.json) |
| Auth | JWT HS256, 30 min access / 14 day refresh with rotation, bcrypt passwords, three roles | [security.py](../backend/app/core/security.py), [auth.py](../backend/app/api/auth.py) |

### 2.1 Call flows as built

**Inbound, Twilio TTS engine**
1. Twilio POSTs `/telephony/voice/inbound`. Tenant resolved from the `To` number against `spa_accounts.twilio_phone_number`, falling back to a workspace user, then to the sole user in dev.
2. `CallLog` row created, `CallSession` written to Redis with the rendered tenant prompt, status callback attached to the live call.
3. Each turn hits `/telephony/voice/respond`: regex pre-filter decides whether to run intent extraction, booking engine stages or confirms, Grok writes the reply, a confirmation-claim guard rewrites any unsupported "you're booked".
4. Sign-off detection hangs up; `finalize_gather_call` writes transcript, summary, language, caller identity.

**Inbound, xAI realtime engine**
1. Same webhook, but returns `<Connect><Stream>` TwiML to `/telephony/media-stream/{call_sid}`.
2. WebSocket handler loads the Redis session (this lookup is the only auth on the stream), opens `wss://api.x.ai/v1/realtime`, sends `session.update` with instructions, voice, five tools, μ-law both ways.
3. Greeting is forced deterministically via `force_message`. Barge-in cancels the response and clears Twilio's buffer. Single playback worker reframes audio into 160-byte frames.
4. Tool calls run the same booking engine. Transcript deltas accumulate; a claim guard cancels any "booked" utterance not backed by a persisted appointment.

**Outbound sales**
1. Super admin POSTs `/telephony/voice/outbound` with lead and objective. Twilio dials; on answer, Grok generates the opener from the sales prompt.
2. Conversation runs on the `<Gather>` loop only. Bookings route to `OutboundSalesAdapter` (Dominic's calendar).

**Browser test call**
Frontend fetches a tenant-scoped Twilio Voice token, connects via the Voice SDK to a TwiML App that POSTs `/telephony/voice/browser-test`, which enters the same inbound flow for that tenant.

### 2.2 Prompts

All prompts live in [grok_service.py](../backend/app/services/grok_service.py):

| Prompt | Used by | Notes |
|---|---|---|
| `VOICE_CALL_BASE_RULES` | shared rules for the text loop | short replies, no markdown, read details back, never claim booking without a `[SYSTEM: ...]` note |
| `SALES_AGENT_PROMPT` | outbound, text loop | "top-tier B2B sales agent for 6DM", books on Dominic's calendar, takes `CALL OBJECTIVE` |
| `SPA_RECEPTIONIST_PROMPT` | inbound, text loop | business name + tenant block + objective |
| `REALTIME_SPA_PROMPT` | inbound, realtime engine | same persona but a four-step tool-driven booking procedure |
| `EXTRACTION_SYSTEM_PROMPT` + `EXTRACTION_FIELDS` | text loop, per turn | JSON-only intent extraction over the last 8 turns |
| `SUMMARY_SYSTEM_PROMPT` | post-call | summary, sentiment, action items, appointment, language, caller identity |

Tenant block (`build_spa_prompt_context`) renders `grok_system_prompt`, service menu, staff, opening hours in the tenant timezone, and an out-of-hours rule. It is rendered once per call and cached on the session. There is no realtime variant of the sales prompt; outbound cannot use the realtime engine today.

### 2.3 Tools available to the model

Defined in [xai_realtime.py:94-224](../backend/app/services/xai_realtime.py#L94-L224), used only by the realtime engine. The text loop has no tools; it relies on post-hoc extraction.

| Tool | Writes? | Purpose |
|---|---|---|
| `check_availability` | no | check one exact slot |
| `propose_appointment` | no | stage the caller's request into the single draft, check slot, return alternatives on conflict |
| `confirm_appointment` | yes | commit the latest draft; no arguments; idempotent on `call_sid:booking_id` |
| `cancel_appointment` | yes | cancel this call's booking or the caller's next upcoming one |
| `start_new_appointment` | no | rotate the intent for an explicitly separate second booking |
| `manage_appointment` | yes | legacy single-step tool; handler still registered but not offered in `VOICE_TOOLS` |

Booking engine guarantees ([appointment_booking_service.py](../backend/app/services/appointment_booking_service.py), [booking_state.py](../backend/app/services/booking_state.py)): one draft per intent, a date change edits the draft or moves the existing row, Postgres advisory locks per call and per slot, a partial unique index on `booking_intent_key`, capacity equal to staff count, business-hours enforcement, service-name canonicalisation against the tenant menu, alternative-slot search capped at 24 probes.

### 2.4 Security model

- **Roles**: `super_admin` (no tenant), `spa_admin`, `spa_staff` (tenant required). DB check constraint enforces role/tenant consistency.
- **Tenant scoping**: `TenantScope` is either a spa `tenant_id` or a sales-workspace `owner_id`. Every scoped route declares `get_tenant_scope`; a test asserts this structurally. Spa scopes on call logs are additionally forced to inbound-only. Contacts and appointments carry a `num_nonnulls(...) = 1` check. Call logs do not.
- **Impersonation**: super admin selects a tenant via `X-Tenant-Id`, validated against a real row. Other roles get 403 on mismatch.
- **Webhooks**: Twilio signature validation exists but is off by default and off in the committed `.env`; forced-on only when `APP_ENV=production`. xAI webhooks use Standard Webhooks HMAC with replay window and key rotation.
- **Secrets at rest**: booking credentials and Google tokens are Fernet-encrypted with a key derived from `SECRET_KEY`; API responses mask secrets.
- **Token revocation**: refresh tokens only. Access tokens remain valid up to 30 minutes after logout.
- **Unauthenticated surfaces**: `/health`, `/health/providers` (makes two outbound API calls per hit), Google OAuth callback (protected by one-time state), Twilio media-stream WebSocket (protected only by session existence in Redis).
- **Frontend**: tokens in `localStorage`, no CSP, refresh token never used for silent refresh.

---

## 3. What is already working

Verified by reading the code paths and by running the backend suite:

```
265 passed, 3 warnings in 6.01s
```

- Inbound routing by dialed number with SIP URI normalisation for trunked calls.
- Both voice engines selectable per tenant via `spa_accounts.voice_engine`.
- Deterministic greeting on the realtime engine, barge-in handling, ordered playback, transcript capture from deltas.
- Draft/confirm booking state machine with idempotency, reschedule-in-place, cross-call reschedule, cancel, second-appointment escape hatch. 18 dedicated tests.
- Google Calendar OAuth connect / list / select / test / disconnect, token refresh and re-encryption.
- Square adapter: catalog search, availability search, customer upsert, create, reschedule with version, cancel with optimistic concurrency, pre-booking re-check.
- Post-call finalisation from three entry points (hang-up, status callback, periodic Twilio sync) with an idempotency rule that also repairs transcript-less rows.
- Periodic Twilio history sync so SIP-trunked calls appear on the dashboard.
- Dashboard: role-filtered navigation, tenant switcher, command center KPIs, call tables with transcript drawer and audio, leads and campaigns pages, spa settings with per-provider credential forms, services CRUD with CSV import, browser test call.
- Fault-tolerant router registration, CORS-safe error handling, sensitive-logger muting.
- Migration chain is linear with one head; a test compiles every migration statement on the Postgres dialect.

---

## 4. Technical debt

### Repository hygiene
- No root `.gitignore`. `.venv/`, `frontend/node_modules/`, `frontend/dist/`, `__pycache__/` and `.env` are all tracked.
- ~3,500 lines uncommitted across 30 files plus 20 untracked source files, including four migrations. A deployed database may already be ahead of `HEAD`.
- Orphaned bytecode `migrations/versions/__pycache__/1f2e3d4c5b6a_voice_engine_default_alignment.cpython-311.pyc` with no source. Any database stamped at that revision will fail `alembic upgrade head`.
- README is UTF-16 with a BOM and mojibake in the tree diagram; it documents nothing about voice, xAI, Railway, or Vercel.
- Line-ending churn: git warns LF will become CRLF on 22 files. No `.gitattributes`.

### Backend
- Two parallel voice stacks with divergent prompts and booking triggers (post-hoc extraction vs tools). The text loop cannot call tools; the realtime path has no sales prompt.
- `services` exists both as JSONB on `spa_accounts` and as a normalised table; `_sync_legacy_services` bridges them.
- `SQUARE_ENVIRONMENT` / `SQUARE_API_VERSION` are read via `getattr(settings, ...)` but not declared in `Settings`, so the `SQUARE_*` lines in `.env` are dead. Square credentials must come from per-spa `booking_config`.
- `OutboundSalesAdapter` never receives a Google connection, so `is_configured` is always false and sales bookings are local-only.
- `AppointmentBookingService.book_appointment` returns a hard-coded success dict without doing anything.
- `_resolve_inbound_target` falls back to "the only user" when a number is unclaimed; fine in dev, dangerous in prod.
- Tests mock the database entirely (`MagicMock` session). SQL invariants are only exercised by applying migrations elsewhere.
- Health endpoint reaches into private `_client` attributes of two services.
- `twilio_sync` `report.created` appears to increment for every call regardless of insert.
- Realtime event-name handling is deliberately tolerant of multiple spellings; it should be tightened now that live traffic has been observed.

### Frontend
- Six dead modules including two `.jsx` files that use a different API base shape and hit a non-existent `/call-logs` path.
- No tests, no ESLint, no Prettier, no CI. An `eslint-disable` comment exists for a linter that is not installed.
- `/spa/calendar` is routed but has no nav entry and no role guard.
- `RequireRole` renders children when `role` is undefined.
- LeadsPage comment promises a debounce that does not exist.
- ~40 `console.log` calls in `BrowserTestCall.tsx` ship to production.
- Vite `define` injects the literal string `undefined` when `NEXT_PUBLIC_API_URL` is unset.
- Frontend Dockerfile runs the dev server; there is no production image.
- Tenant switch triggers a full `window.location.reload()`.

---

## 5. Missing credentials, configuration and documentation

| Item | State | Impact |
|---|---|---|
| `SECRET_KEY` | not set in `.env`; falls back to `CHANGE_ME_IN_PRODUCTION_32_CHAR_MIN` | weak JWT key locally; Fernet key derived from it |
| `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_TWIML_APP_SID` | absent | browser test call returns 503 unless set on Railway |
| `XAI_VOICE_WEBHOOK_SECRET` | empty while `XAI_VOICE_ENABLED=true` | SIP webhook path unauthenticated outside production; fatal 500 in production |
| `SALES_GOOGLE_CALENDAR_ID` / `GOOGLE_CALENDAR_ID` | absent | outbound sales adapter has no calendar id |
| Google OAuth connection for the sales workspace | no mechanism exists | sales bookings cannot write to Google even with an id |
| `SQUARE_LOCATION_ID` | placeholder `your...` | dead anyway (see above) |
| `TWILIO_VALIDATE_SIGNATURE` | `false` | webhooks forgeable in non-production |
| `PUBLIC_BASE_URL` for Railway, `DATABASE_URL`, `REDIS_URL` | set only in Railway dashboard, undocumented | no runbook to reproduce the environment |
| xAI Voice Agent entitlement and number registration | not held | engine C cannot be used |
| Twilio account tier | trial-era comments (recording disabled) | unknown whether upgraded |
| Documentation | only `docs/google-calendar.md` | no architecture doc, no deployment runbook, no tenant onboarding guide, no env var reference, no prompt changelog |

---

## 6. Risks, ranked

1. **Leaked secrets in git history.** Twilio auth token, xAI API key, Google client secret, Square access token. Rotate all four, add a root `.gitignore`, purge history or accept the leak and rely on rotation.
2. **Single key for JWT and credential encryption with no production validator.** A rotation to fix item 1 will wipe every tenant's booking credentials and Google tokens unless a re-encryption path is built first.
3. **Uncommitted production-shaping work.** If the Railway build is from `HEAD`, the deployed code lacks the Square adapter, services API, idempotency migrations and booking state machine, while the database may have been migrated by a local run. Divergence is unverified.
4. **Media stream WebSocket authenticated only by Redis session presence.** Anyone who guesses or observes a live CallSid within the 1h TTL can attach a socket and inject or receive audio. Twilio cannot send headers, but a signed token in the stream URL would close this.
5. **Unauthenticated `/health/providers`** discloses provider configuration and triggers billable upstream calls.
6. **Voice name is unvalidated by xAI.** A typo silently changes the caller's experience; only detectable by listening.
7. **Sales bookings do not reach the calendar.** The B2B product's core promise is currently a local-only row.
8. **No frontend tests or lint, mocked DB in backend tests.** Regressions in SQL constraints or UI flows are caught only in production.
9. **Access tokens not revocable for 30 minutes**, stored in `localStorage`, no CSP.
10. **Dev fallback that attributes unclaimed inbound numbers to the sole user** could misroute a real call to the wrong workspace if a second tenant's number is misconfigured.

---

## 7. Recommended phased architecture

Assumption on terminology: the codebase today has two conversational personas, the Spa Receptionist and the 6DM Sales Agent, plus a post-call analyst. This plan treats those as the three brains. If the intended third brain is something else, the router contract below is persona-agnostic and accommodates it without structural change.

### Phase 0: stabilise (1 week)

1. Rotate every leaked credential. Add root `.gitignore`, `.gitattributes`, untrack `.venv`, `node_modules`, `dist`, `__pycache__`, `.env`.
2. Introduce `BOOKING_ENCRYPTION_KEY` separate from `SECRET_KEY`, with a one-time re-encryption command, then rotate `SECRET_KEY`. Add a Pydantic validator that refuses the default key and unsigned webhooks when `APP_ENV=production`.
3. Commit the outstanding work in reviewable slices: migrations, booking engine, Square adapter, services API, frontend. Confirm Railway's revision matches `alembic current`.
4. Delete the orphaned migration bytecode. Delete the six dead frontend modules.
5. Add a signed, short-lived token to the media-stream URL and require it in the WebSocket handler. Put `/health/providers` behind super-admin auth.
6. Write `docs/environment.md` (every env var, where it is set) and `docs/deploy.md` (Railway, Vercel, migration procedure).

### Phase 1: shared foundation

Goal: one call runtime that both voice engines and all brains sit on.

- **`CallContext`** replaces the ad hoc `CallSession` fields: identity (call id, provider ids), tenant scope, persona id, channel (`twilio_tts`, `twilio_media_stream`, `xai_sip`, `browser`), locale, and a typed `BookingState`. Keep Redis as the store; version the JSON.
- **`VoiceChannel` interface**: `speak(text)`, `listen()`, `barge_in()`, `hangup()`. Implementations wrap the `<Gather>` loop, the media bridge, and the SIP driver. The brain never knows which one is active.
- **`ToolRegistry`**: the five booking tools become the single source of truth, exposed to the realtime engine as function schemas and to the text loop via a lightweight tool-call parse of the extraction output. This removes the two-stack divergence.
- **`BookingEngine`** stays as is; expose it only through the registry.
- **Event bus** (in-process first): `call.started`, `turn.completed`, `tool.invoked`, `booking.committed`, `call.finalized`. Finalisation, sync, and analytics subscribe instead of being called from three places.
- **Observability**: keep the `TURN LATENCY` log lines, but also emit them as structured events with `call_id`, `tenant_id`, `persona`, `channel`. Add a per-call trace id.
- **Testing**: add a Postgres-backed integration test job (docker service in CI) that applies migrations and exercises the SQL constraints the unit suite mocks away. Add ESLint, Prettier, Vitest to the frontend with a minimal smoke test per page.

### Phase 2: router

Goal: one entry point that decides tenant, persona, channel and brain for every call.

- **Inbound router**: dialed number -> tenant -> `voice_engine` -> persona. Replace the "sole user" fallback with an explicit `unclaimed` outcome that plays a fixed message and logs an alert.
- **Outbound router**: campaign or lead -> persona `sales` -> channel. Make the realtime engine available to outbound by adding a realtime sales prompt.
- **Persona resolution** from a `personas` table (or JSONB on `spa_accounts` for now): system prompt template, tool allow-list, voice id, language, guardrail set, booking adapter policy. The three brains are three persona rows plus code hooks, not three code paths.
- **Guardrail pipeline** as ordered middleware on the reply stream: confirmation-claim guard, sign-off detector, out-of-hours rule, PII redaction for logs. Today these are scattered regexes in two files.
- **Channel selection policy**: tenant default, with per-call override for A/B tests and a circuit breaker that falls back from the realtime engine to `<Gather>` when xAI is unhealthy.

### Phase 3: three brains

Each brain is a persona plus a small module implementing `open(ctx)`, `on_turn(ctx, utterance)`, `tools()`, `close(ctx)`.

1. **Spa Receptionist** (inbound). Exists. Work: move to `ToolRegistry`, add FAQ answering from the services table, add a `take_message` tool for follow-up-required outcomes, add language switching using `primary_language`.
2. **6DM Sales Agent** (outbound). Exists as a prompt only. Work: realtime prompt variant, lead qualification fields as tools (`record_qualification`, `schedule_presentation`, `mark_not_interested`), fix the Google connection for Dominic's calendar via a sales-workspace OAuth connection, campaign pacing and retry rules, compliance (consent, do-not-call list, time-of-day windows).
3. **Post-Call Analyst** (offline). Exists as `analyze_call`. Work: run on the event bus after finalisation, produce structured outcome, sentiment, action items, lead score, and a quality rubric on the agent's own behaviour (did it read back, did it claim a booking without one). Feed results to the dashboard and to prompt regression tests.

Shared: a prompt registry with versioning and an eval harness that replays recorded transcripts through each brain and asserts tool calls and guardrail hits.

### Phase 4: dashboard

- **Live call monitor**: subscribe to the event bus over SSE or WebSocket; show active calls, current turn latency, brain and channel in use, booking state. `LiveCallMonitor.tsx` was the start of this and is currently dead.
- **Call review**: transcript with tool-call markers, audio if recorded, analyst rubric, one-click "flag for prompt review".
- **Tenant onboarding wizard**: number assignment, persona and voice selection, business hours, services import, calendar connection, test call. Replace the page reload on tenant switch with a query-key invalidation once a data-fetching library is adopted.
- **Sales operations**: campaign builder, dial queue with pacing, lead pipeline with analyst scores, Dominic's calendar view backed by the real Google connection.
- **Platform health**: provider status (authenticated), migration revision, queue depths, per-tenant error rates, cost per call.
- **Production build**: multi-stage frontend Dockerfile or rely on Vercel, add CSP headers in `vercel.json`, move tokens to httpOnly cookies with a silent-refresh flow.

### Sequencing

Phase 0 is a prerequisite for everything and should ship before any new feature work. Phase 1 and Phase 4's live monitor can proceed in parallel since the event bus serves both. Phase 2 depends on Phase 1's `CallContext`. Phase 3 brains can be migrated one at a time onto the router, starting with the Receptionist because it has the most test coverage.
