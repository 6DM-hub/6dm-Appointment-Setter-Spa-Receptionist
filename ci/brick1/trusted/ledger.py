#!/usr/bin/env python3
"""Findings ledger for the Brick 1 repair-and-verification workflow.

One JSON file (ledger/findings.json) records every finding and every repair
attempt. The rules it enforces:

* Turns: Codex owns a finding first. Each owner gets up to TWO attempts per
  turn. Two failed attempts hand the finding to the other agent, with the
  evidence of every earlier attempt (`handoff`). If that agent also fails
  twice, it goes back the other way. Alternation continues; the owner can
  stop it at any time.
* An attempt is only recorded with: the exact commit, the diff against the
  base commit, reproduction steps, the author's own test results, and an
  INDEPENDENT verification of that same commit (summary.json written by
  verify_commit.py). A verification of any other commit is rejected.
* The author's own tests never count as approval. A finding becomes
  `verified` only when independent verification of the attempt's commit
  passed; it becomes `approved` only by the owner (`approve --by owner`).
* ChatGPT reviews (`review`) are attached as text; they inform the next
  attempt but do not change status.

Usage:
  ledger.py open R5-F1 --title "..." --severity P0 --source "Codex R5 review" --repro repro.md
  ledger.py next R5-F1
  ledger.py attempt R5-F1 --agent codex --commit <sha> --base <sha> --repo <path> \
        --repro repro.md --author-tests author.log --verification /tmp/verify-<sha>
  ledger.py review R5-F1 --by chatgpt --notes review.md
  ledger.py handoff R5-F1 --out /tmp/handoff-R5-F1
  ledger.py status
  ledger.py approve R5-F1 --by owner
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "ledger" / "findings.json"
EVIDENCE = HERE / "ledger" / "evidence"
AGENTS = ("codex", "claude")
ATTEMPTS_PER_TURN = 2


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {"rules": {"attempts_per_turn": ATTEMPTS_PER_TURN, "first_owner": "codex",
                      "approval": "independent verification PASS on the exact commit, then owner approval"},
            "findings": {}}


def save(data: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(data, indent=2) + "\n")


def next_owner(finding: dict) -> tuple[str, str]:
    """Whose turn it is, and why."""
    if finding["status"] in {"verified", "approved"}:
        return "owner", f"finding is {finding['status']}: no repair turn"
    owner, used = "codex", 0
    for attempt in finding["attempts"]:
        if attempt["agent"] != owner:
            # Only possible after a handoff; keep counting in the new turn.
            owner, used = attempt["agent"], 0
        used += 1
        if used >= ATTEMPTS_PER_TURN and attempt["verification"]["verdict"] != "PASS":
            owner, used = AGENTS[1 - AGENTS.index(owner)], 0
    left = ATTEMPTS_PER_TURN - used
    return owner, f"{owner} has {left} of {ATTEMPTS_PER_TURN} attempts left in this turn"


def cmd_open(a, data):
    if a.id in data["findings"]:
        sys.exit(f"{a.id} already exists")
    data["findings"][a.id] = {
        "id": a.id, "title": a.title, "severity": a.severity, "source": a.source,
        "repro": Path(a.repro).read_text() if a.repro else "", "opened": now(),
        "status": "open", "attempts": [], "reviews": [],
    }
    save(data)
    print(f"opened {a.id}; next: {next_owner(data['findings'][a.id])[1]}")


def cmd_next(a, data):
    print(next_owner(data["findings"][a.id])[1])


def cmd_attempt(a, data):
    f = data["findings"][a.id]
    owner, why = next_owner(f)
    if a.agent != owner:
        sys.exit(f"refused: it is not {a.agent}'s turn ({why})")
    summary_path = Path(a.verification) / "summary.json"
    if not summary_path.exists():
        sys.exit("refused: no independent verification (summary.json) for this attempt")
    summary = json.loads(summary_path.read_text())
    full = subprocess.check_output(["git", "rev-parse", a.commit], cwd=a.repo, text=True).strip()
    if summary.get("commit") != full:
        sys.exit(f"refused: verification is for {summary.get('commit')}, not {full}")
    n = len(f["attempts"]) + 1
    store = EVIDENCE / a.id / f"attempt-{n}-{a.agent}"
    store.mkdir(parents=True, exist_ok=True)
    diff = subprocess.check_output(["git", "diff", f"{a.base}..{full}"], cwd=a.repo, text=True)
    (store / "changes.diff").write_text(diff)
    shutil.copy(a.repro, store / "repro.md")
    shutil.copy(a.author_tests, store / "author-tests.log")
    for name in ("summary.json", "SUMMARY.md"):
        shutil.copy(Path(a.verification) / name, store / f"verification-{name}")
    f["attempts"].append({
        "n": n, "agent": a.agent, "commit": full, "base": a.base, "recorded": now(),
        "evidence": str(store.relative_to(HERE)),
        "verification": {"verdict": summary["verdict"], "commit": summary["commit"],
                         "suites": {k: {"passed": v.get("passed"), "failed": v["failures"] + v["errors"]}
                                    for k, v in summary.get("suites", {}).items()},
                         "refusals": summary.get("refusals", [])},
    })
    if summary["verdict"] == "PASS":
        f["status"] = "verified"
        print(f"{a.id} attempt {n} by {a.agent}: independent verification PASS -> verified "
              "(awaiting owner approval)")
    else:
        print(f"{a.id} attempt {n} by {a.agent}: independent verification FAIL; next: {next_owner(f)[1]}")
    save(data)


def cmd_review(a, data):
    f = data["findings"][a.id]
    f["reviews"].append({"by": a.by, "recorded": now(), "after_attempt": len(f["attempts"]),
                         "notes": Path(a.notes).read_text()})
    save(data)
    print(f"review by {a.by} attached to {a.id}")


def cmd_handoff(a, data):
    f = data["findings"][a.id]
    owner, why = next_owner(f)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# Handoff: {f['id']} ({f['severity']}) to {owner.upper()}", "", f"**{f['title']}**",
             f"Source: {f['source']}. Status: {f['status']}. {why}.", "",
             "## Reproduction", "", f["repro"] or "(see attempts)", "", "## Earlier attempts", ""]
    for att in f["attempts"]:
        v = att["verification"]
        lines.append(f"- Attempt {att['n']} by {att['agent']}: commit `{att['commit']}` "
                     f"-> independent verification **{v['verdict']}** {v['refusals'] or ''}")
        src = HERE / att["evidence"]
        shutil.copytree(src, out / src.name, dirs_exist_ok=True)
    for rev in f["reviews"]:
        lines += ["", f"## Review by {rev['by']} (after attempt {rev['after_attempt']})", "", rev["notes"]]
    lines += ["", "## Rules for this attempt", "",
              "- Work on a branch from the workbench head; one commit per attempt.",
              "- Do not modify Codex test modules or tools/brick1_workflow/; add new tests instead.",
              "- Simulated providers only; no production calendar writes, merge, push to main or deploy.",
              "- Record the attempt with ledger.py attempt ... --verification <verify_commit.py output>."]
    (out / "HANDOFF.md").write_text("\n".join(lines) + "\n")
    print(f"handoff for {a.id} to {owner} written to {out}")


def cmd_status(a, data):
    print(f"{'ID':10} {'SEV':4} {'STATUS':9} {'ATTEMPTS':8} NEXT")
    for f in data["findings"].values():
        print(f"{f['id']:10} {f['severity']:4} {f['status']:9} {len(f['attempts']):<8} {next_owner(f)[1]}")


def cmd_approve(a, data):
    f = data["findings"][a.id]
    if a.by != "owner":
        sys.exit("refused: only the owner approves")
    if f["status"] != "verified":
        sys.exit(f"refused: {a.id} is {f['status']}, not verified")
    f["status"], f["approved"] = "approved", now()
    save(data)
    print(f"{a.id} approved by owner")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open"); p.add_argument("id"); p.add_argument("--title", required=True)
    p.add_argument("--severity", required=True); p.add_argument("--source", required=True); p.add_argument("--repro")
    p = sub.add_parser("next"); p.add_argument("id")
    p = sub.add_parser("attempt"); p.add_argument("id"); p.add_argument("--agent", choices=AGENTS, required=True)
    for opt in ("--commit", "--base", "--repo", "--repro", "--author-tests", "--verification"):
        p.add_argument(opt, required=True)
    p = sub.add_parser("review"); p.add_argument("id"); p.add_argument("--by", required=True)
    p.add_argument("--notes", required=True)
    p = sub.add_parser("handoff"); p.add_argument("id"); p.add_argument("--out", required=True)
    sub.add_parser("status")
    p = sub.add_parser("approve"); p.add_argument("id"); p.add_argument("--by", required=True)
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a, load())


if __name__ == "__main__":
    main()
