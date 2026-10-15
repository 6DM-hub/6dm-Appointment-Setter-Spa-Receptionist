# Prompt for ChatGPT: review a Brick 1 finding and a proposed repair

Paste the prompt below into ChatGPT. Attach the finding's `HANDOFF.md` and the attempt folder: `changes.diff`, `repro.md`, `author-tests.log` and `verification-SUMMARY.md`. Save ChatGPT's answer as a file and attach it to the ledger with:

```bash
ledger.py review <ID> --by chatgpt --notes <file>
```

---

You are reviewing a repair to **CARA One**, a multi-tenant AI phone receptionist. CARA books, moves and cancels appointments in external calendars (Square, Google) and in its own calendar. It runs on two voice paths: realtime (xAI) and legacy Twilio.

Brick 1 is about one thing: **CARA must never create, move or cancel an appointment the caller did not clearly authorize, and must never report a change as done unless the calendar of record confirmed it.** This applies to every tenant and industry.

The attachments are:
- the finding;
- the repair diff;
- the reproduction steps;
- the author's own test results;
- an independent verification of the exact commit. All providers in it are simulated.

Please do the following.

1. **Root cause.** State in two or three sentences what you think the root cause is. Say whether the diff fixes that cause or only the reproduced symptom.
2. **Challenge assumptions.** For each assumption the repair makes (about message order, transcript wording, provider replies, retries, concurrency, tenants, languages), say whether it holds on both voice paths and for every calendar provider.
3. **Bypasses.** List up to five concrete inputs or sequences that could still produce an unauthorized write or a false success after this repair. Give each as caller and assistant turns, or provider replies, precise enough to become a test.
4. **Regressions.** List legitimate flows the change might now block or loop on: non-English calls, moves, additional appointments, required caller name, card policy.
5. **Verdict.** Choose one, with one sentence of reasoning:
   - "likely fixed";
   - "fixes the symptom only";
   - "introduces a new risk".

Do not approve or reject the brick. Only the owner approves, and only after independent verification passes. Do not suggest testing against live customer calendars.
