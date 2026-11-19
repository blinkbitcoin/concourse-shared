"""Run shared/ci/tasks/nodejs-audit.sh against stub package managers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "shared/ci/tasks/nodejs-audit.sh"


class NodejsAuditTests(unittest.TestCase):
    def run_audit(self, lockfiles, tool, exit_code, level="high"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            for lockfile in lockfiles:
                (repo / lockfile).write_text("")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"
            for name in ("pnpm", "yarn"):
                stub = bin_dir / name
                code = exit_code if name == tool else 99
                stub.write_text(
                    f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit {code}\n'
                )
                stub.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "REPO_ROOT": str(repo),
                    "LEVEL": level,
                },
                capture_output=True,
                text=True,
            )
            invoked = calls.read_text().splitlines() if calls.exists() else []
            return result, invoked

    def test_pnpm_findings_fail_the_audit(self):
        # pnpm exits 1 for findings at or above --audit-level; that must not
        # be read as a yarn severity bitmask (1 = info only).
        for level in ("high", "critical"):
            result, invoked = self.run_audit(["pnpm-lock.yaml"], "pnpm", 1, level)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(invoked, [f"pnpm audit --prod --audit-level={level}"])

    def test_pnpm_clean_audit_passes(self):
        result, invoked = self.run_audit(["pnpm-lock.yaml"], "pnpm", 0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, ["pnpm audit --prod --audit-level=high"])

    def test_yarn_bitmask_threshold(self):
        cases = (
            ("high", 0, True),
            ("high", 1 | 2 | 4, True),
            ("high", 8, False),
            ("high", 16, False),
            ("high", 4 | 16, False),
            ("critical", 8, True),
            ("critical", 16, False),
        )
        for level, code, passes in cases:
            with self.subTest(level=level, code=code):
                result, invoked = self.run_audit(["yarn.lock"], "yarn", code, level)
                self.assertEqual(result.returncode == 0, passes, result.stdout + result.stderr)
                self.assertEqual(invoked, [f"yarn audit --groups dependencies --level {level}"])

    def test_yarn_lock_takes_precedence(self):
        result, invoked = self.run_audit(["yarn.lock", "pnpm-lock.yaml"], "yarn", 8)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invoked, ["yarn audit --groups dependencies --level high"])

    def test_missing_lockfile_fails(self):
        result, invoked = self.run_audit([], "pnpm", 0)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(invoked, [])


if __name__ == "__main__":
    unittest.main()
