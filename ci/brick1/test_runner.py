"""Stdlib contract checks for CI status and trust boundaries; no app deps."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('runner', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class CIContract(unittest.TestCase):
    def test_rejects_refs_and_shell_payloads(self):
        for value in ('main', 'HEAD', '--help', 'a'*39, 'a'*41, 'z'*40, '$(echo unsafe)'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.validate_commit(value)
        self.assertEqual(runner.validate_commit('A'*40), 'a'*40)

    def test_startup_error_is_blocked_not_product_failure(self):
        self.assertEqual(runner.assess({'verdict':'FAIL','suites':{'full-backend':{'missing':True}}})[0], 'BLOCKED')

    def test_frozen_test_refusal_is_failure(self):
        self.assertEqual(runner.assess({'refusals':['Frozen test changed']})[0], 'FAIL')

    def test_skips_cannot_look_like_pass(self):
        self.assertEqual(runner.assess({'verdict':'PASS','suites':{'full-backend':{'skipped':1}}})[0], 'FAIL')

    def test_expected_failing_controls_do_not_invalidate_pass(self):
        summary={'verdict':'PASS','suites':{'full-backend':{'skipped':0},'postgres-CONTROL-locks_removed':{'failures':3}}}
        self.assertEqual(runner.assess(summary)[0], 'PASS')

    def test_trusted_file_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'referee.py').write_text('trusted')
            (root/'trusted-sha256.json').write_text(json.dumps({'referee.py': hashlib.sha256(b'trusted').hexdigest()}))
            runner.check_integrity(root)
            (root/'referee.py').write_text('tampered')
            with self.assertRaises(ValueError):
                runner.check_integrity(root)

    def test_status_artifact_explicitly_withholds_owner_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            code=runner.write_status(path, 'BLOCKED', 'dependency unavailable', 'a'*40)
            self.assertEqual(code, 2)
            record=json.loads((path/'ci-status.json').read_text())
            self.assertEqual(record['verdict'], 'BLOCKED')
            self.assertIn('UNAPPROVED', record['approval'])

if __name__=='__main__':
    unittest.main()
