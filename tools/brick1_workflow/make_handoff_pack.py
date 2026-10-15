#!/usr/bin/env python3
"""Build a self-contained ZIP so an agent WITHOUT repository access (for
example Codex in a chat, or Codex Cloud before the branch is pushed) can
attempt a finding.

The pack contains:
  - repo.bundle            the workbench branch (git clone repo.bundle)
  - trusted-workflow/      this folder as of the bundled commit (the referee)
  - HANDOFF.md + attempts  from `ledger.py handoff` (if --finding is given)
  - TASK.md                a ready-to-paste task for the receiving agent
No .env, credentials, customer data or dependency folders are included.

  python make_handoff_pack.py --repo . --branch claude/brick1-workbench \
      --finding R5-F1 --agent codex --out /tmp/codex-R5-F1.zip
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

TASK = """# Task for {agent}: {finding}

Repository access is through `repo.bundle` (no network needed):

    git clone repo.bundle cara && cd cara && git checkout -b {agent}/{slug}-attempt <base>
    (base = {head})

Read `AGENTS.md` in the repository root first; its rules are binding.
{handoff_line}
Deliver, for THIS attempt:
1. ONE commit on your branch; give its full SHA and `git diff {head}..<sha>`.
2. Reproduction: a new test that fails at {head} and passes at your commit.
3. Your own results: the full backend suite (`cd backend && python -m pytest -q`).
4. Independent verification of your exact commit, run from the TRUSTED copy
   in this pack (not from your branch):
       python trusted-workflow/verify_commit.py --repo cara --commit <sha> \\
           --python $(which python) --out verify-<sha> --label "{finding} by {agent}"
   Return the whole verify-<sha>/ folder.
5. A `git bundle create attempt.bundle {head}..<branch>` so the commit can be
   fetched and re-verified independently.

Simulated providers only. Do not merge, push to shared branches, deploy, or
touch any real calendar or customer data. Do not modify Codex test modules or
tools/brick1_workflow/. Passing your own tests is not approval.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="claude/brick1-workbench")
    ap.add_argument("--finding", default="")
    ap.add_argument("--agent", default="codex", choices=["codex", "claude"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    head = subprocess.check_output(["git", "rev-parse", a.branch], cwd=repo, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip():
        sys.exit("refused: commit or stash local changes first (the pack must match a commit)")
    stage = Path(tempfile.mkdtemp(prefix="cara-handoff-")) / "pack"
    stage.mkdir(parents=True)
    # Full history of the branch, so it clones without any other repository.
    subprocess.check_call(["git", "bundle", "create", "-q", str(stage / "repo.bundle"), a.branch], cwd=repo)
    # The referee as of the bundled commit, not as of any later attempt.
    subprocess.check_call(f"git archive {head} tools/brick1_workflow | tar -x -C {stage}", shell=True, cwd=repo)
    shutil.move(str(stage / "tools" / "brick1_workflow"), str(stage / "trusted-workflow"))
    shutil.rmtree(stage / "tools")
    handoff_line = ""
    if a.finding:
        subprocess.check_call([sys.executable, str(HERE / "ledger.py"), "handoff", a.finding,
                               "--out", str(stage / "handoff")])
        handoff_line = "Then read `handoff/HANDOFF.md`: the finding, every earlier attempt and its evidence.\n"
    slug = (a.finding or "brick1").lower()
    (stage / "TASK.md").write_text(TASK.format(agent=a.agent, finding=a.finding or "Brick 1",
                                               slug=slug, head=head, handoff_line=handoff_line))
    out = Path(a.out).resolve()
    archive = shutil.make_archive(str(out.with_suffix("")), "zip", stage.parent, "pack")
    shutil.rmtree(stage.parent)
    print(f"{archive}  (branch {a.branch} @ {head})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
