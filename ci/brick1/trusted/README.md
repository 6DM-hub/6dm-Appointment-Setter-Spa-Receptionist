# Brick 1 repair-and-verification workflow

This folder is the **referee** for Brick 1, which is about safe booking behaviour for every tenant and industry, on both realtime voice and legacy telephony.

- It is not application code.
- Repair commits must not modify it.
- Verification always runs from a trusted copy taken before the change being judged.

| File | Purpose |
|---|---|
| `verify_commit.py` | Independent verification of one exact commit, in a fresh clone. It refuses the commit if a Codex test module changed. It then runs the full suite, each Codex module, the 64 scenarios (0 soft MISS required), any extra tests, and optionally PostgreSQL plus two controls that must fail. Output: `SUMMARY.md`, `summary.json`, JUnit and logs. |
| `ledger.py` | The findings ledger (`ledger/findings.json`). It enforces whose turn it is, records each attempt with its commit, diff, reproduction steps, the author's own tests and independent verification, attaches ChatGPT reviews, writes handoffs, and records owner approval. |
| `make_handoff_pack.py` | A ZIP for an agent with no repository access. It contains the git bundle, the trusted workflow, `HANDOFF.md` and a ready-to-paste `TASK.md`. |
| `CHATGPT_REVIEW_PROMPT.md` | The prompt ChatGPT uses to review a finding and a proposed repair. |
| `codex_test_manifest.json` | SHA-256 of Codex's frozen test modules. |
| `scenarios/` | The 64 independent scenarios Codex supplied, with `SHA256SUMS`. |
| `pg_controls/` | Conftests that remove the advisory locks or the change fence, to prove the PostgreSQL tests can fail. |

## Flow for one finding

1. **Open the finding:** `ledger.py open <ID> --title … --severity P0 --source "Codex R5" --repro repro.md`.
2. **Codex attempt.** Codex makes one commit on a branch from `claude/brick1-workbench` (see `/AGENTS.md`).
3. **Independent verification** of that exact commit, from a trusted copy:
   `verify_commit.py --repo … --commit <sha> --out verify-<sha>`.
4. **Record the attempt:**
   `ledger.py attempt <ID> --agent codex --commit <sha> --base <sha> --repo … --repro … --author-tests … --verification verify-<sha>`.
   A verification of a different commit is refused.
5. **ChatGPT review.** ChatGPT reviews the attempt with `CHATGPT_REVIEW_PROMPT.md`. Its answer is attached with `ledger.py review <ID> --by chatgpt --notes …`.
6. **Two failed attempts hand over.** After two failed attempts, `ledger.py next` names Claude. `ledger.py handoff <ID> --out …` collects both attempts' evidence. Claude's attempts go through the same steps 2–5.
7. **Turns alternate.** If Claude also fails twice, the finding goes back to Codex, again with all the evidence: `make_handoff_pack.py --finding <ID> --agent codex`. Turns keep alternating until the owner stops them.
8. **Verified, then approved.** A finding is `verified` only when independent verification passes on the attempt's commit. Only the owner marks it `approved`.

## Limits (honest)

- **Owner approval is a ledger entry.** Anyone with shell access could type `--by owner`. The guard is that only the owner should run it.
- **Simulated providers only.** Passing verification here is not a live Square, Google, Twilio or model test.
- **Moving files.** This folder does not post to any chat group or reach other agents. Moving files between agents happens wherever the agents are connected; see the report that came with this branch.
