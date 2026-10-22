"""Trust admission tests; no package changes or network requests."""
import contextlib
import importlib.util
import io
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "images/trust/verify.py"
spec = importlib.util.spec_from_file_location("trust", SCRIPT)
trust = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trust)


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.failure = None
        self.package = "install ok installed\n" + trust.MINIMUM
        self.release = 'ID=debian\nVERSION_ID="12"\n'
        self.enter = contextlib.ExitStack()
        self.addCleanup(self.enter.close)
        self.enter.enter_context(patch.object(trust.Path, "read_text", side_effect=lambda: self.release))
        self.bundle = self.enter.enter_context(patch.object(trust.Path, "is_file", return_value=True))
        self.uid = self.enter.enter_context(patch.object(trust.os, "geteuid", return_value=0))
        self.command = self.enter.enter_context(patch.object(trust.subprocess, "run", side_effect=self.run_command))
        self.enter.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def run_command(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(kwargs["env"], {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
            "DEBIAN_FRONTEND": "noninteractive",
        })
        self.assertEqual(kwargs["timeout"], 900)
        if self.failure and self.failure[0](args):
            return subprocess.CompletedProcess(args, self.failure[1], "", self.failure[2])
        if args[0] == "dpkg-query":
            return subprocess.CompletedProcess(args, 0, self.package, "")
        if any(a.startswith("RUSTUP_DIST_SERVER=") for a in args):
            return subprocess.CompletedProcess(args, 1, "", "invalid peer certificate: Other(OtherError(CertDistrusted))")
        if args[-1].endswith("-emudhra.we-have-a-problem.com/"):
            if args[0] == "/usr/local/bin/node":
                return subprocess.CompletedProcess(args, 1, "", trust.NODE_ERRORS[0])
            return subprocess.CompletedProcess(args, 60, "", trust.ISSUER_ERRORS[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_refresh_then_all_twelve_and_healthy(self):
        trust.qualify(refresh=True)
        self.assertEqual([x[0] for x in self.calls[:3]], ["apt-get", "apt-get", "update-ca-certificates"])
        self.assertIn("APT::Update::Error-Mode=any", self.calls[0])
        self.assertIn("--no-remove", self.calls[1])
        self.assertEqual(self.calls[1][-3:], ("install", "ca-certificates", "curl"))
        self.assertEqual([x[-1] for x in self.calls if x[0] == "/usr/bin/curl"],
                         [f"https://{h}/" for h in trust.HOSTS] + ["https://deb.debian.org/"])
        self.assertEqual(len(set(trust.HOSTS)), 12)
        for call in self.calls:
            if call[0] == "/usr/bin/curl":
                self.assertEqual(call[1], "-q")
                self.assertIn(trust.BUNDLE, call)
                self.assertNotIn("--insecure", call)
                self.assertNotIn("--location", call)

    def test_verify_never_mutates(self):
        trust.qualify()
        self.assertFalse(any(x[0] in ("apt-get", "update-ca-certificates") for x in self.calls))

    def test_wrong_os_or_release_stops_before_commands(self):
        for release in ('ID=ubuntu\nVERSION_ID="12"', 'ID=debian\nVERSION_ID="22.04"'):
            self.release = release
            with self.assertRaisesRegex(RuntimeError, "Only Debian"):
                trust.qualify(refresh=True)
        self.assertEqual(self.calls, [])

    def test_unprivileged_refresh_is_blocked(self):
        self.uid.return_value = 1000
        with self.assertRaisesRegex(RuntimeError, "root"):
            trust.qualify(refresh=True)
        self.assertEqual(self.calls, [])

    def test_package_refresh_failures_stop_before_tls(self):
        for command, operation in (("apt-get", "update"), ("apt-get", "curl"), ("update-ca-certificates", "update-ca-certificates")):
            self.calls.clear()
            self.failure = (lambda a: a[0] == command and a[-1] == operation, 100, "private diagnostics")
            with self.assertRaisesRegex(RuntimeError, "image build remains blocked"):
                trust.qualify(refresh=True)
            self.assertFalse(any(a[0] == "/usr/bin/curl" for a in self.calls))

    def test_package_absent_incomplete_or_old_is_blocked(self):
        for package in ("", "deinstall ok config-files\n" + trust.MINIMUM, "install ok installed"):
            self.package = package
            with self.assertRaisesRegex(RuntimeError, "fully installed"):
                trust.qualify()
        self.package = "install ok installed\n20260601~24.04.1"
        self.failure = (lambda a: a[0] == "dpkg", 1, "")
        with self.assertRaisesRegex(RuntimeError, "dpkg failed"):
            trust.qualify()
        self.failure = (lambda a: a[0] == "dpkg-query", 1, "")
        with self.assertRaisesRegex(RuntimeError, "dpkg-query failed"):
            trust.qualify()

    def test_bundle_required(self):
        self.bundle.return_value = False
        with self.assertRaisesRegex(RuntimeError, "bundle is missing"):
            trust.qualify()

    def test_every_chain_must_be_rejected_for_issuer_trust(self):
        for host in trust.HOSTS:
            for status, error in ((0, ""), (6, "DNS failed"), (28, "timeout"),
                                  (60, "certificate has expired"), (60, "hostname mismatch")):
                with self.subTest(host=host, status=status, error=error):
                    self.failure = (lambda a: a[-1] == f"https://{host}/", status, error)
                    with self.assertRaisesRegex(RuntimeError, host):
                        trust.qualify()

    def test_recognized_issuer_failures_and_healthy_failure(self):
        for error in trust.ISSUER_ERRORS:
            self.failure = (lambda a: a[-1].endswith("-emudhra.we-have-a-problem.com/"), 60, error)
            trust.qualify()
        self.failure = (lambda a: a[-1] == "https://deb.debian.org/", 60, "untrusted")
        with self.assertRaisesRegex(RuntimeError, "curl failed"):
            trust.qualify()

    def test_cli_modes_and_sanitized_failures(self):
        for mode in ("verify", "refresh"):
            with patch.object(sys, "argv", [str(SCRIPT), mode]), patch.object(trust, "qualify") as qualify:
                trust.main()
                qualify.assert_called_once_with(refresh=mode == "refresh", node=False, rustup=False)
        with patch.object(sys, "argv", [str(SCRIPT), "verify", "--node"]), patch.object(trust, "qualify") as qualify:
            trust.main()
            qualify.assert_called_once_with(refresh=False, node=True, rustup=False)
        with patch.object(sys, "argv", [str(SCRIPT), "verify", "--rustup"]), patch.object(trust, "qualify") as qualify:
            trust.main()
            qualify.assert_called_once_with(refresh=False, node=False, rustup=True)
        for error in (OSError("missing binary"), RuntimeError("blocked"), subprocess.TimeoutExpired("curl", 30)):
            with patch.object(sys, "argv", [str(SCRIPT), "verify"]), patch.object(trust, "qualify", side_effect=error), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    trust.main()
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("Trust gate failed", stderr.getvalue())

    def test_node_all_chains_and_healthy(self):
        for error in trust.NODE_ERRORS:
            self.calls.clear()
            self.failure = (lambda a: a[0] == "/usr/local/bin/node" and "emudhra" in a[-1], 1, error)
            trust.qualify(node=True)
            self.assertEqual([a[-1] for a in self.calls if a[0] == "/usr/local/bin/node"],
                             [f"https://{h}/" for h in trust.HOSTS] + ["https://registry.npmjs.org/"])
        self.failure = None
        trust.qualify(node=True)

    def test_node_rejects_every_other_outcome(self):
        for host in trust.HOSTS:
            for status, error in ((0, ""), (1, "ENOTFOUND"), (1, "ABORT_ERR"),
                                  (1, "CERT_HAS_EXPIRED"), (1, "ERR_TLS_CERT_ALTNAME_INVALID"),
                                  (2, "CERT_REVOKED")):
                self.failure = (lambda a: a[0] == "/usr/local/bin/node" and a[-1] == f"https://{host}/", status, error)
                with self.assertRaisesRegex(RuntimeError, "Node inconclusive"):
                    trust.qualify(node=True)
        self.failure = (lambda a: a[-1] == "https://registry.npmjs.org/", 1, "UNABLE_TO_GET_ISSUER_CERT_LOCALLY")
        with self.assertRaisesRegex(RuntimeError, "node failed"):
            trust.qualify(node=True)

    def test_rustup_checks_all_chains_then_real_update_servers(self):
        trust.qualify(rustup=True)
        calls = [a for a in self.calls if a[0] == "/usr/bin/env"]
        self.assertEqual(len(calls), 13)
        for host, call in zip(trust.HOSTS, calls):
            self.assertIn(f"RUSTUP_DIST_SERVER=https://{host}", call)
            self.assertIn(f"RUSTUP_UPDATE_ROOT=https://{host}", call)
        self.assertNotIn("RUSTUP_DIST_SERVER", " ".join(calls[-1]))

    def test_rustup_rejects_all_other_outcomes(self):
        for host in trust.HOSTS:
            for status, error in ((0, ""), (1, "DNS failure"), (1, "timeout"),
                                  (1, "expired"), (2, "CertDistrusted")):
                self.failure = (lambda a: f"RUSTUP_DIST_SERVER=https://{host}" in a, status, error)
                with self.assertRaisesRegex(RuntimeError, "Rustup inconclusive"):
                    trust.qualify(rustup=True)
        self.failure = (lambda a: a[0] == "/usr/bin/env" and not any(v.startswith("RUSTUP_DIST_SERVER=") for v in a), 1, "download failure")
        with self.assertRaisesRegex(RuntimeError, "env failed"):
            trust.qualify(rustup=True)

    def test_entrypoint_help_never_runs_commands(self):
        with patch.object(sys, "argv", [str(SCRIPT), "--help"]):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
