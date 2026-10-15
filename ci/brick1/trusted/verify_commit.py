#!/usr/bin/env python3
"""Independent verification of ONE exact commit for CARA One Brick 1.

Run this from a TRUSTED copy of tools/brick1_workflow (for example, the copy
on the workbench branch's verified head, or an unzipped handoff pack), never
from the commit under test: the scenarios, the Codex test manifest and the
network barrier are taken from this script's own folder, so a repair cannot
weaken the checks that judge it.

What it does, in a fresh clone (the source repository is never modified):
  1. checks out exactly --commit and refuses a dirty tree;
  2. refuses the commit if any Codex test module differs from the manifest;
  3. runs, with every IPv4/IPv6 connection blocked and dummy Twilio values:
       - the full backend suite,
       - each Codex module on its own (so its count is visible),
       - the 64 supplied scenarios (soft MISS count must be 0),
       - any extra independent test files passed with --extra-tests,
       - optionally the PostgreSQL race/fence tests and both controls;
  4. writes JUnit XML, logs, SUMMARY.md and summary.json into --out.

Exit code 0 only if everything required passed. Providers are simulated by
the test suites themselves; nothing here contacts Square, Google, Twilio or
a model API.

Example:
  python verify_commit.py --repo /path/to/repo --commit 140d920... \
      --python /path/to/venv/bin/python --out /tmp/verify-140d920
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
NETWORK_ENV = {
    "TWILIO_ACCOUNT_SID": "AC00000000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "x",
    "TWILIO_PHONE_NUMBER": "+15551234567",
    "TWILIO_VALIDATE_SIGNATURE": "false",
}
PROXY_VARS = ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy")


def sh(cmd, cwd=None, env=None, log=None) -> int:
    with open(log, "w") if log else open(os.devnull, "w") as out:
        return subprocess.run(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT).returncode


def junit_counts(path: Path) -> dict:
    if not path.exists():
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0, "missing": True}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for s in suites:
        for key in total:
            total[key] += int(s.get(key, 0) or 0)
    total["passed"] = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="path or URL of the git repository")
    ap.add_argument("--commit", required=True, help="exact commit SHA to verify")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--python", default=sys.executable, help="Python 3.11 with backend requirements")
    ap.add_argument("--extra-tests", nargs="*", default=[], type=Path,
                    help="additional independent test files (e.g. a reviewer's new challenges)")
    ap.add_argument("--pg-url", default=os.environ.get("CARA_TEST_POSTGRES_URL"),
                    help="disposable PostgreSQL URL; enables the race/fence tests and controls")
    ap.add_argument("--label", default="", help="e.g. 'R5-F1 attempt 2 by codex'")
    args = ap.parse_args()

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cara-verify-"))
    checkout = work / "checkout"
    summary: dict = {
        "label": args.label, "commit": args.commit, "started": datetime.now(timezone.utc).isoformat(),
        "python": subprocess.check_output([args.python, "--version"], text=True).strip(),
        "network": "IPv4/IPv6 connections blocked; providers simulated by the tests",
        "suites": {}, "refusals": [],
    }

    # 1. exact commit, clean tree
    if sh(["git", "clone", "-q", str(args.repo), str(checkout)], log=out / "clone.log"):
        summary["refusals"].append("clone failed (see clone.log)")
    elif sh(["git", "checkout", "-q", args.commit], cwd=checkout, log=out / "checkout.log"):
        summary["refusals"].append(f"commit {args.commit} not found (see checkout.log)")
    if not summary["refusals"]:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        summary["commit"] = head
        summary["tree"] = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=checkout, text=True).strip()
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout, text=True).strip():
            summary["refusals"].append("checkout is not clean")

    # 2. Codex tests byte-identical
    if not summary["refusals"]:
        manifest = json.loads((HERE / "codex_test_manifest.json").read_text())["backend/tests"]
        identity = {}
        for name, expected in manifest.items():
            path = checkout / "backend" / "tests" / name
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            identity[name] = {"expected": expected, "actual": actual, "identical": actual == expected}
            if actual != expected:
                summary["refusals"].append(f"Codex test {name} was changed or removed")
        summary["codex_test_identity"] = identity

    if summary["refusals"]:
        return finish(summary, out, work, ok=False)

    backend = checkout / "backend"
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env.update(NETWORK_ENV)
    env["PYTHONPATH"] = str(HERE)  # makes verification_network_block importable
    base = [args.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "verification_network_block",
            "--tb=short", "--show-capture=no", "-c", "pytest.ini", "--rootdir=."]

    def run(name, targets, extra_env=None):
        xml = out / f"{name}.junit.xml"
        e = dict(env, **(extra_env or {}))
        code = sh(base + [f"--junitxml={xml}"] + [str(t) for t in targets], cwd=backend, env=e,
                  log=out / f"{name}.log")
        summary["suites"][name] = {"exit": code, **junit_counts(xml)}
        return code

    # 3. suites
    run("full-backend", ["tests"])
    for name in json.loads((HERE / "codex_test_manifest.json").read_text())["backend/tests"]:
        run("codex__" + name.removesuffix(".py"), [f"tests/{name}"])
    notes = out / "scenario-notes"
    notes.mkdir(exist_ok=True)
    run("scenarios-64", [HERE / "scenarios"], {"B1_LOG_DIR": str(notes)})
    notes_log = notes / "notes.log"
    miss = sum("MISS" in line for line in notes_log.read_text().splitlines()) if notes_log.exists() else None
    summary["scenario_soft_miss"] = miss
    for i, extra in enumerate(args.extra_tests):
        run(f"extra-{i}-{extra.stem}", [extra.resolve()])

    if args.pg_url:
        pg_env = {"CARA_TEST_POSTGRES_URL": args.pg_url}
        run("postgres", ["tests_postgres"], pg_env)
        for control in ("locks_removed", "fence_removed"):
            folder = work / f"control_{control}"
            folder.mkdir()
            for test in (backend / "tests_postgres").glob("test_*.py"):
                shutil.copy(test, folder / test.name)
            shutil.copy(HERE / "pg_controls" / f"{control}_conftest.py", folder / "conftest.py")
            run(f"postgres-CONTROL-{control}", [folder], pg_env)
    else:
        summary["postgres"] = "not run (no --pg-url)"

    required = [n for n in summary["suites"] if not n.startswith("postgres-CONTROL")]
    ok = all(summary["suites"][n]["exit"] == 0 and summary["suites"][n]["tests"] > 0 for n in required)
    ok = ok and miss == 0
    if args.pg_url:  # a control that does not fail proves nothing
        ok = ok and all(summary["suites"][f"postgres-CONTROL-{c}"]["failures"] > 0
                        for c in ("locks_removed", "fence_removed"))
    return finish(summary, out, work, ok=ok)


def finish(summary: dict, out: Path, work: Path, *, ok: bool) -> int:
    summary["verdict"] = "PASS" if ok else "FAIL"
    summary["finished"] = datetime.now(timezone.utc).isoformat()
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    lines = [f"# Independent verification: {summary['verdict']}", "",
             f"- Label: {summary.get('label') or '-'}",
             f"- Commit: `{summary.get('commit')}`  tree `{summary.get('tree', '-')}`",
             f"- Runtime: {summary.get('python')}; {summary['network']}", ""]
    for refusal in summary["refusals"]:
        lines.append(f"- **REFUSED:** {refusal}")
    if summary["suites"]:
        lines += ["| Suite | Passed | Failed | Errors | Skipped | Exit |", "|---|---:|---:|---:|---:|---:|"]
        for name, s in summary["suites"].items():
            lines.append(f"| {name} | {s.get('passed', 0)} | {s['failures']} | {s['errors']} | {s['skipped']} | {s['exit']} |")
        lines += ["", f"Scenario soft MISS: {summary.get('scenario_soft_miss')}",
                  f"PostgreSQL: {summary.get('postgres', 'run (controls must fail)')}"]
    lines += ["", "Simulated providers only. Passing this is necessary, not sufficient: owner approval is separate."]
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    print((out / "SUMMARY.md").read_text())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
