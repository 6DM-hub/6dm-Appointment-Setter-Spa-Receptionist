# Instructions for coding agents (Codex, Claude) on CARA One Brick 1

You are working on **Brick 1: safe booking behaviour**. It covers every tenant and industry, on both the realtime voice path (`backend/app/services/xai_realtime.py`) and the legacy Twilio path (`backend/app/api/v1/telephony.py`). The workflow, ledger and verifier are in `tools/brick1_workflow/` (see its README).

## Hard rules

- **Branches.** Work only on a branch created from `claude/brick1-workbench` (for example `codex/brick1-<finding>-attempt<n>`). Make one commit per attempt.
- **Never** merge, push to `main` or `codex/cara-grok-ara`, deploy, change Railway or Vercel, or write to a real calendar, Square account, Twilio number or customer record.
- **Simulated providers only.** Tests use FakeSquare (`backend/tests/square_world.py`), simulated Google functions, SQLite, or a throwaway local PostgreSQL. Never report a simulated test as a real integration test.
- **Codex test modules are frozen.** These files must stay byte-identical (`tools/brick1_workflow/codex_test_manifest.json`):
  - `backend/tests/test_codex_brick1_adversarial.py`
  - `backend/tests/test_codex_brick1_tenant_boundaries.py`
  - `backend/tests/test_codex_brick1_r2_independent.py`
  - `backend/tests/test_codex_r3_challenges.py`
  - `backend/tests/test_codex_r4_challenges.py`

  Add new tests in new files. Never weaken, skip, delete or relabel a failing case.
- **Do not edit `tools/brick1_workflow/`** in a repair commit. It is the referee.
- **No tenant-specific code.** Never hard-code Ruby Spa, spa services, or any one tenant or industry into the shared booking engine.
- **Line endings.** Many source files use CRLF. Keep each file's existing line endings.

## Every repair attempt must include

1. The exact commit SHA and the diff against the base commit.
2. Reproduction steps for the finding: a failing test before the change and a passing test after.
3. The author's own test results: the full backend suite at least.
4. An **independent** verification of that same commit, using `verify_commit.py` run from a trusted copy of `tools/brick1_workflow`. The author's own tests never count as approval.

## Commands

From `backend/`, with Python 3.11 and `requirements.txt` installed:

```bash
export TWILIO_ACCOUNT_SID=AC00000000000000000000000000000000 TWILIO_AUTH_TOKEN=x \
       TWILIO_PHONE_NUMBER=+15551234567 TWILIO_VALIDATE_SIGNATURE=false
python -m pytest -q                                      # full suite (1723 at 140d920)
python -m pytest -q tests/test_codex_r4_challenges.py    # one module
```

Independent verification of a commit, from the repository root:

```bash
cp -r tools/brick1_workflow /tmp/trusted-workflow     # trusted copy, taken BEFORE your change
python /tmp/trusted-workflow/verify_commit.py --repo . --commit <sha> \
    --python "$(which python)" --out /tmp/verify-<sha> --label "<finding> attempt <n> by <agent>"
```

The PostgreSQL tests are optional: pass `--pg-url postgresql+asyncpg://...` for a disposable database only.

## Ledger and turns

`tools/brick1_workflow/ledger.py` enforces the turn rules:
- Codex has up to 2 attempts per finding.
- After 2 failures, Claude gets 2 attempts, with the evidence from Codex's attempts.
- After 2 more failures, the finding goes back to Codex, and turns keep alternating.

A finding is `verified` only when independent verification passes on the attempt's commit. Only the owner marks it `approved`.
