# Cara voice preservation audit — October 9, 2026

No deployment or production configuration change was performed. This audit verifies source preservation; it does not replace staging or an audible phone test.

## Exact versions and no-overwrite check

Repository: `6DM-hub/6dm-Appointment-Setter-Spa-Receptionist`. Railway production branch `codex/cara-grok-ara` remains `aa458ce53efb477029cd6790c57ea1a92b8a8724`, deployment `8f792b59-195d-41cd-8a39-f13717edc465`, SUCCESS, with no staged changes. Reviewed candidate `codex/cara-staging-review-20261009` was `ad459e751af06a16d5e944cb34a36d36e9f4bba1`, whose direct parent is the current production commit.

The audit follow-up on that candidate adds only the omitted aiosqlite test dependency and this report. It does not change runtime voice code. The final published SHA is recorded in the workspace report.

The complete Git trees were compared. The original candidate changed/added 38 files and deleted none. Media Bridge, Twilio Service, telephony routing, voice_config, voice_setup, Grok persona/instructions, application voice settings, greeting/pause/outbound/recovery regression files are exact production blobs. In xai_realtime, 84 methods/functions are semantically unchanged, including greeting, dispatch, pause/interruption, forced speech, grounding and recovery helpers; five methods add enhancement state/reporting hooks, and two enhancement helpers are added. No method is removed. All 45 booking_state functions are unchanged; only generic card wording constants change to stop applying one business's 24-hour policy to every business.

The booking service preserves 51 functions; changes are the optional enhancement flag in routing, a no-notification guard for local test sessions, and explicit test adapter paths in stage/confirm. Normal provider creation and the final exact-slot recheck retain the production implementation.

Immediately before any eventual production ref update, re-read its head. If it is no longer aa458ce, merge/re-audit against the newer commit and rerun regressions. Use a ref lease; do not overwrite the production branch or replace it from the old source archive.

## All 13 failures: exact causes

An isolated backend was constructed with every tracked production app/test file matching the aa458ce Git blob and the identical pytest configuration. Result: **25 passed, the exact same 13 failed**. The failures are pre-existing relative to the pending manager/dashboard/enhancement candidate. They are voice/booking test maintenance issues, not unrelated infrastructure failures.

| Test | Actual cause |
|---|---|
| caller_name_and_card_speech: test_confirmation_speech_mentions_sms_only_when_it_was_sent | Calls the success-speech helper four times on the same session, external booking ID and time. After the first, spoken_booking_confirmation intentionally returns an empty string. Test incorrectly expects another failure-SMS/confirmation line from that same booking. Each SMS scenario needs a fresh session; duplicates must remain silent. |
| date_grounding_state: test_a_date_named_then_time_only_turn_resolves_that_date | Spoken October 1 resolves to October 1, 2027 after October 1, 2026 has passed. The fixture sends October 1, 2026, so temporal grounding rejects the mismatch. |
| date_grounding_state: test_b_corrected_date_resolves_to_the_new_date_not_the_original | Corrected spoken October 2 resolves to 2027; hardcoded tool date is 2026. |
| date_grounding_state: test_d_a_conversational_date_mention_cannot_be_distinguished_from_a_booking_one | Same October 1 year rollover mismatch. The incidental-date limitation documented by this test is still a separate product concern; this failure does not prove that limitation was repaired. |
| realtime_response_lifecycle: test_one_response_cannot_chain_an_unbounded_run_of_availability_probes | Authoritative availability cancels the old model response. Later tools from that response return cancelled, not the old ungrounded_time/tool_chain_limit categories. Provider execution remains blocked. |
| realtime_response_lifecycle: test_per_response_cap_still_refuses_a_second_grounded_probe_in_one_response | Second tool belongs to an already-cancelled response; cancelled precedes the older too_many_attempts guard. |
| realtime_response_lifecycle: test_response_create_is_not_sent_while_the_same_response_is_still_open | Expects one deferred response.create. Authoritative results now use xAI force_message and cancel old model continuations, so zero response.create is correct for this path. |
| tool_continuation_loop: test_successful_probe_gets_an_unrestricted_continuation | Expects an unrestricted response.create after successful availability. The deployed fix instead delivers the backend's authoritative result through force_message and stops autonomous model probing. |
| tool_continuation_loop: test_date_established_earlier_then_time_only_turn_is_grounded | Fourth October 1, 2026 fixture with the spoken date now resolving to 2027. |
| tool_continuation_loop: test_tool_result_and_response_done_race_yields_one_continuation | Same outdated response.create expectation; one authoritative forced-speech item replaces a model continuation. |
| tool_continuation_loop: test_model_ignoring_tool_choice_and_repeating_the_same_probe_is_still_blocked | Repeated rejected probes enter the callback-permission state. Test then supplies new details and expects available, but the recovery flow correctly remains awaiting_callback_consent until that pending question is answered. |
| tool_continuation_loop: test_model_varying_arguments_each_time_is_eventually_stopped_by_the_chain_depth_cap | Expects every later result to remain tool_chain_limit. The deployed recovery transitions to awaiting_callback_consent and blocks subsequent tools instead. |
| tool_continuation_loop: test_mixed_success_and_rejection_in_one_response_leaves_no_stale_continuation | First authoritative success cancels the response; later tool from it is cancelled, not ungrounded_time. No unrestricted/stale continuation should be resurrected. |

Diagnostic confirmation: using a September 30, 2026 clock against the unchanged production code makes all four expired date-fixture cases pass (4 passed). This was a scratch-only diagnostic, not a production clock change. No booking or silence-consent guard was weakened.

## Regression results

- **424 relevant regressions passed**: xAI session/voice, Media Bridge, retry routing, outbound, Twilio, receptionist identity, one greeting/startup, confirmation pause/interruption/card playback, hold tone, last-call and reschedule recovery, offered-slot grounding, booking intent/conversation, Square availability/timezone/staff, secure card entry/status/no spoken cards, manager and enhancements.
- Fresh complete candidate suite: **694 passed, 13 pre-existing failed**, four dependency/test-secret warnings. No new failing test.
- Exact production comparison: **25 passed, same 13 failed**.
- Frozen-clock diagnostic: **4 passed**.
- No live booking, outreach, payment, call or provider configuration was initiated.

The test dependency aiosqlite was present locally but absent from the first candidate requirements manifest. The audit adds it under test dependencies while preserving every existing production requirement. This addresses reproducibility of the new database tests, not phone audio generation.

## Audio provider trace

Incoming xai_realtime establishments return Twilio Connect/Stream TwiML, not Say/Gather. Outgoing sessions select xai_realtime when XAI_REALTIME_ENABLED is true and return the same stream path. A missing outbound realtime session returns 503 rather than quietly opening the text-to-speech path.

TwilioMediaBridge opens the xAI realtime WebSocket, configures resolve_xai_voice(XAI_VOICE_ID), supplies Ara, and relays xAI audio deltas into Twilio's ordered playback queue. Input and output are audio/pcmu at 8000 Hz, with no resampling. Caller speech cancels queued speech where required. The greeting watchdog's alternate opening remains in the same Grok session. xAI force_message for authoritative slots/card policy/confirmation uses that same configured voice; the fact that the backend supplies exact text does not mean Twilio renders it.

Legacy Twilio Say/Gather with the configured TwiML voice (default alice) remains for establishments/sessions deliberately configured as twilio_tts. It is not an automatic xAI-connection-error fallback. If the realtime bridge fails, it logs/finalizes the session rather than switching providers. Do not confuse the existing legacy path with the active Ara call route.

Production's deployment-specific startup log reports release aa458ce, voice ara, catalogue ara_verified, actual inbound/outbound realtime audio probes ready (12082 bytes each), audio/pcmu at 8000 Hz, Redis connected, configured Twilio number matched. Startup setup refuses success unless XAI_REALTIME_ENABLED is true and Ara is selected. Credential values remain redacted; no setting was changed.

No new incoming or outgoing phone call was initiated during this audit. Startup probes and automated routing tests establish the configured path, not audible CARE-uh, natural tone, live pauses or the success of every phone call.

## Exact proposed production scope

The candidate contains: owner Ask Cara/preferences and establishment-master approval; test-only reactivation proposal/outbox/reply/shared booking/results/audit; enhancement selection and explicit extra-cost consent; Square single-variation/bundle price/duration/provider/slot verification; original-booking recovery and reporting/privacy/retention; associated dashboard/API/models; business-specific generic card wording and sensitive-request flag; Railway production test guards; regression tests and test dependency; documentation.

Database changes: a101_cara_manager -> a102_business_master -> a103_smart_enhancements, following d1e2f3a4b5c6. Backup and staging migration verification are required. Production must explicitly set APP_ENV=production and assign the establishment master through the audited operation. Preserve current Ara, xAI endpoint/key, Twilio routing, database/Redis references and working booking credentials. Enhancements remain disabled by default; enabling them requires reviewed catalog mappings. No live campaign sender is enabled.

Live campaign sending/scheduling, unsupported external package writes, balance/membership readback, atomic multi-provider/resource additions and waitlist/reminder completion are not claimed as delivered. Separate massage plus facial/brow-wax additions require a supported provider bundle, not guessed capacity.

## Deployment verdict and live test

**No newer production voice fix would be overwritten by the reviewed candidate. Safe for isolated staging review; not cleared for production yet.** Remaining gates: existing 13 tests need maintained expectations, successful Linux frontend build/browser QA (local esbuild spawn EPERM), staging PostgreSQL migration, sandbox/provider booking readback, secure-card completion, and authorized inbound/outbound Ara audio tests. Staging environment/access and dedicated test connections remain needed. Production approval is withheld pending these gates.

During an authorized test: hear one actual-business greeting and CARE-uh; allow silence at a booking question and confirm no consent/card speech occurs; interrupt a follow-up and ensure Cara lets you finish; select a real offered slot and give your name without a repeated approval question; check service/duration/provider/local time and exact final availability; accept/decline one optional mapped enhancement and its extra cost; complete a secure card link off-call if required; hear one success statement only after provider success (pending card must remain identified); independently verify the appointment in Square and no duplicate. An unavailable/stalled request should use waiting sound and a single callback-permission question, without guaranteed staff-contact claims. Use a dedicated approved outbound test to verify Ara there too.
