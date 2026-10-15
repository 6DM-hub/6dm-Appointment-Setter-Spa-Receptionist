# Shared Brick 1 verification

This is a testing-only GitHub Actions workflow. It sets up Python 3.11 and the
workbench requirements, then runs the unchanged trusted verifier against an
exact commit. No deployment job, production credentials, automatic merge, or
application repairs are included. A green result never approves Brick 1.

## First activation

The infrastructure branch is `codex/brick1-ci-setup`. Its push runs an initial
environment/test job. The Round 5 target `140d9200788b8dbb84e2d765f670eb54bfef5408`
must exist on an authorized remote verification branch before booking tests
can run. The supplied bundle's repair history was **not published** by this
change. A missing commit produces BLOCKED rather than a booking failure.

The new workflow must be merged into the default branch before GitHub shows
its normal **Run workflow** button. This infrastructure-only merge is distinct
from releasing or approving the repair branch. Review the CI diff first.
No default branch or production branch is changed by the setup branch itself.

## Routine use after activation

1. Put an authorized candidate repair commit on a separate remote test branch.
2. In **Actions → Brick 1 independent verification → Run workflow**, select the
   reviewed infrastructure/default branch and paste the full commit SHA.
3. Leave disposable PostgreSQL enabled for race/fence coverage and controls.
4. Review the job summary and the `brick1-evidence-*` artifact. Share the run
   link with Codex and Claude; connected agents can retrieve the same evidence.
   This does not cause separate chat sessions to message or run autonomously.

Backend pull requests targeting main also trigger verification after this
workflow is installed on main. Fork PR commits may be unfetchable: they remain
BLOCKED until available as an authorized same-repository test commit. The
trusted files come from the base commit, never from the PR's application tree.
Only use a reviewed workflow branch for manual dispatch: it owns the referee.

## Evidence and safety boundaries

- Network is used for runner provisioning, package installation, source fetch,
  and evidence upload. Test execution runs in an isolated Linux network
  namespace with no IP egress. The supplied pytest barrier is also unchanged.
- PostgreSQL 16 is throwaway, local, and Unix-socket-only. Tests destroy its
  tables. No database secrets or calendar secrets are referenced.
- Full backend tests, all five frozen modules, 64 scenarios with zero MISS,
  the three extra modules, PostgreSQL, and negative controls are run by the
  trusted verifier. Frozen checksums are checked before execution.
- New extra tests are **unexecuted candidates** from the blocked local review;
  they must be assessed on their first real execution. They are not confirmed
  failures. No frozen test is edited to accommodate them.
- Artifacts retain raw verifier summaries, JUnit, logs, runtime versions, and
  explicit PASS/FAIL/BLOCKED status. A missing JUnit is an environment BLOCKED
  result; changed frozen tests or skipped required checks cannot become PASS.
- Author test results alone do not count as independent approval. The owner
  controls approval. Review the trusted workflow changes independently.
- Only the workflow's SHA input is accepted; it must be 40 hexadecimal
  characters. It is passed through an environment variable and subprocess
  arguments rather than interpolated as shell code.

## Local checks

`python ci/brick1/test_runner.py -v` uses only the standard library and checks
CI status classification, SHA validation and integrity tamper detection. This
does not exercise application booking behavior. Full execution requires the
CI environment or a provisioned Python 3.11 environment.
