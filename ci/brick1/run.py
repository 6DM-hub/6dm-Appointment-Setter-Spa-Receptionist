#!/usr/bin/env python3
"""CI adapter for the unchanged trusted Brick 1 verifier (stdlib only).

Exit 0: complete simulated verification PASS; 1: FAIL; 2: BLOCKED.
This script never installs packages, fetches commits, or changes the source repo.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent

def validate_commit(value):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', value):
        raise ValueError('Target must be an exact 40-character hexadecimal commit SHA.')
    return value.lower()

def check_integrity(root):
    manifest = json.loads((root / 'trusted-sha256.json').read_text())
    if not manifest or not all(isinstance(k, str) and isinstance(v, str) for k, v in manifest.items()):
        raise ValueError('Trusted-file manifest is empty or invalid.')
    for name, expected in manifest.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f'Missing or unsafe trusted file: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Trusted file changed: {name}')
    return manifest

def assess(summary):
    if summary.get('refusals'):
        return 'FAIL', '; '.join(summary['refusals'])
    suites = summary.get('suites', {})
    required = {k: v for k, v in suites.items() if not k.startswith('postgres-CONTROL')}
    if not required or any(v.get('missing') for v in required.values()):
        return 'BLOCKED', 'Test startup failed or JUnit evidence is missing; inspect logs.'
    if any(v.get('skipped', 0) for v in required.values()):
        return 'FAIL', 'Required verification contains skipped tests.'
    if summary.get('verdict') != 'PASS':
        return 'FAIL', 'Trusted verifier did not pass; inspect JUnit and logs.'
    return 'PASS', 'All requested simulated suites and controls passed. Owner approval is separate.'

def write_status(out, status, reason, target, **details):
    out.mkdir(parents=True, exist_ok=True)
    record = dict(verdict=status, reason=reason, target=target,
        runtime=sys.version, recorded_at=datetime.now(timezone.utc).isoformat(),
        approval='UNAPPROVED; only the owner can approve Brick 1', **details)
    (out / 'ci-status.json').write_text(json.dumps(record, indent=2) + '\n')
    markdown = f'# Brick 1 CI: {status}\n\nTarget: `{target}`\n\n{reason}\n\nSimulated providers only. Brick 1 remains unapproved.\n'
    (out / 'CI-STATUS.md').write_text(markdown)
    print(markdown)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(markdown)
    return {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2}[status]

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', required=True, type=Path)
    ap.add_argument('--commit', required=True)
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--pg-socket', type=Path)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = args.commit
    try:
        target = validate_commit(target)
        check_integrity(HERE)
        if sys.version_info[:2] != (3, 11):
            raise ValueError('Python 3.11 is required; actual runtime is ' + sys.version.split()[0])
        source = args.repo.resolve()
        result = subprocess.run(['git', '-C', str(source), 'rev-parse', '--verify', target + '^{commit}'],
            text=True, capture_output=True)
        if result.returncode or result.stdout.strip() != target:
            raise ValueError('Exact target commit is not available in the fetched repository. '
                'Publish an authorized verification branch containing it; no repair code is uploaded by this workflow.')
        identity = json.loads((HERE / 'trusted/codex_test_manifest.json').read_text())['backend/tests']
        for filename, expected in identity.items():
            blob = subprocess.run(['git', '-C', str(source), 'show', f'{target}:backend/tests/{filename}'], capture_output=True)
            if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != expected:
                return write_status(out, 'FAIL', f'Frozen test missing or changed: {filename}', target)
        import importlib.metadata
        # Fail as a setup blocker, not as a booking failure, if the environment is incomplete.
        for package in ('pytest', 'pytest-asyncio', 'fastapi', 'sqlalchemy', 'twilio', 'aiosqlite', 'asyncpg'):
            importlib.metadata.version(package)
        extras = sorted((HERE / 'extra-tests').glob('test_*.py'))
        if not extras:
            raise ValueError('Independent extra test files are missing.')
        pg_args = []
        if args.pg_socket:
            socket = args.pg_socket.resolve()
            if not str(socket).startswith('/tmp/cara-brick1-pg-') or not (socket / '.s.PGSQL.5432').exists():
                raise ValueError('PostgreSQL must use the workflow-created disposable Unix socket.')
            pg_args = ['--pg-url', f'postgresql+asyncpg://postgres@/cara_brick1?host={socket}']
        env = {k: v for k, v in os.environ.items() if k.lower() not in
            ('http_proxy', 'https_proxy', 'all_proxy') and k not in
            ('CARA_TEST_POSTGRES_URL', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH')}
        # pytest checks presence, so '0' would disable required asyncio plugins too.
        env.pop('PYTEST_DISABLE_PLUGIN_AUTOLOAD', None)
        command = [sys.executable, str(HERE / 'trusted/verify_commit.py'), '--repo', str(source),
            '--commit', target, '--python', sys.executable, '--out', str(out / 'verify-140d920'),
            '--label', 'Shared CI independent verification', *pg_args, '--extra-tests', *map(str, extras)]
        (out / 'command.json').write_text(json.dumps(command, indent=2))
        with (out / 'verifier-console.log').open('w') as log:
            code = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
        summary = json.loads((out / 'verify-140d920/summary.json').read_text())
        status, reason = assess(summary)
        # Defense against a contradictory or incomplete verifier result.
        if status == 'PASS' and (code != 0 or summary.get('commit') != target):
            status, reason = 'FAIL', 'Verifier exit status or target commit contradicts its summary.'
        return write_status(out, status, reason, target, verifier_exit=code,
            postgres='run with disposable Unix socket and controls' if pg_args else 'NOT RUN')
    except (OSError, ValueError, KeyError, ImportError) as exc:
        return write_status(out, 'BLOCKED', str(exc), target)

if __name__ == '__main__':
    sys.exit(main())
