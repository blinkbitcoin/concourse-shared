import contextlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import tarfile
import types
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("candidate_clients", ROOT / "qualification/clients.py")
CLIENTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENTS)


class CandidateClientsTests(unittest.TestCase):
    def setUp(self):
        self.peers = patch.object(CLIENTS.chains, "pinned_peers", return_value={h: "127.0.0.1" for h in CLIENTS.HOSTS})
        self.snapshots = patch.object(CLIENTS.chains, "snapshot", return_value={"matches": True})
        self.peers.start()
        self.snapshots.start()
        self.addCleanup(self.peers.stop)
        self.addCleanup(self.snapshots.stop)

    def test_clean_environment(self):
        with patch.dict("os.environ", {"AWS_SECRET_ACCESS_KEY": "do-not-forward", "HTTPS_PROXY": "invalid"}):
            env = CLIENTS.environment(Path("/tmp/sandbox"))
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["GH_TOKEN"], CLIENTS.DUMMY_TOKEN)
        self.assertEqual(env["HOME"], "/tmp/sandbox")
        self.assertEqual(env["CLOUDSDK_STORAGE_MAX_RETRIES"], "0")

    def test_execute(self):
        with patch.object(CLIENTS.subprocess, "run", return_value=types.SimpleNamespace(
                returncode=1, stdout="out", stderr="err")) as run:
            self.assertEqual(CLIENTS.execute(["tool"], {"HOME": "sandbox"}, "/tmp"), (1, "outerr"))
            self.assertEqual(run.call_args.kwargs["timeout"], 60)
            self.assertEqual(run.call_args.kwargs["env"], {"HOME": "sandbox"})
            self.assertEqual(CLIENTS.execute(["tool"], {}, "/tmp", stderr_only=True), (1, "err"))
        for error in [FileNotFoundError(), subprocess.TimeoutExpired("tool", 60)]:
            with self.subTest(error=type(error)), patch.object(CLIENTS.subprocess, "run", side_effect=error):
                self.assertEqual(CLIENTS.execute(["tool"], {}, "/tmp"), (None, type(error).__name__))

    def test_rejection_classifier(self):
        for error in CLIENTS.ISSUER_ERRORS:
            self.assertTrue(CLIENTS.accepted(1, error.upper(), negative=True))
        for code, error in [(None, "timeout"), (0, CLIENTS.ISSUER_ERRORS[0]),
                            (1, "DNS error"), (1, "HTTP 401"), (1, "timed out")]:
            self.assertFalse(CLIENTS.accepted(code, error, negative=True))
        for error in ["certificate has expired", "certificate is expired", "not yet valid",
                      "hostname mismatch", "doesn't match", "does not match",
                      "no alternative certificate subject name"]:
            self.assertFalse(CLIENTS.accepted(1, CLIENTS.ISSUER_ERRORS[0] + error, negative=True))
        self.assertTrue(CLIENTS.accepted(1, "assert_hostname=self.assert_hostname\n"
                                        "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate",
                                        negative=True))
        for response in ["HTTP 401", "HTTPError 403", "HTTP/1.1 500", "HTTP: 404"]:
            self.assertFalse(CLIENTS.accepted(1, response + ": " + CLIENTS.ISSUER_ERRORS[0], negative=True))
        prefix = "Error: failed listing installations: unable to POST to https://example.test: "
        self.assertTrue(CLIENTS.accepted(1, prefix + CLIENTS.ISSUER_ERRORS[0], negative=True,
                                        auth_control="ghtoken"))
        for output in [CLIENTS.ISSUER_ERRORS[0], prefix + "HTTP 500 " + CLIENTS.ISSUER_ERRORS[0],
                       "Error: failed listing installations: unexpected status code: 404"]:
            self.assertFalse(CLIENTS.accepted(1, output, negative=True, auth_control="ghtoken"))

    def test_healthy_classifier(self):
        self.assertTrue(CLIENTS.accepted(0, "ok"))
        self.assertFalse(CLIENTS.accepted(1, "error"))
        for code, text in [(None, "HTTP 401 Bad credentials"), (0, "HTTP 401 Bad credentials"),
                           (1, "HTTP 403 Bad credentials"), (1, "HTTP 401 unrelated")]:
            self.assertFalse(CLIENTS.accepted(code, text, auth_control="gh"))
        self.assertTrue(CLIENTS.accepted(1, "HTTP 401 Bad credentials", auth_control="gh"))
        for client, good in [("gcloud", "HTTPError 401: Invalid Credentials"),
                             ("ghtoken", "Error: failed listing installations: unexpected status code: 401"),
                             ("ghtoken", "Error: failed listing installations: unexpected status code: 404")]:
            self.assertTrue(CLIENTS.accepted(1, good, auth_control=client))
            for code, message in [(0, good), (1, "401"), (1, "not authenticated")]:
                self.assertFalse(CLIENTS.accepted(code, message, auth_control=client))

    def test_chart_healthy_quota_is_tls_only(self):
        prefix = "Error: GET https://api.github.com/repos/helm/chart-releaser/releases/tags/v1.6.0: "
        for message in ["403 API rate limit exceeded", "403 API rate limit of 60 still exceeded until 2026-10-24"]:
            self.assertTrue(CLIENTS.accepted(1, prefix + message, auth_control="cr"))
            self.assertFalse(CLIENTS.accepted(1, prefix + message, negative=True, auth_control="cr"))
        for output in ["403 Forbidden", "API rate limit exceeded", prefix + "403 Forbidden",
                       prefix.replace("api.github.com", "unrelated.test") + "403 API rate limit exceeded"]:
            self.assertFalse(CLIENTS.accepted(1, output, auth_control="cr"))
        self.assertTrue(CLIENTS.accepted(0, "Index did not change", auth_control="cr"))

    def test_chart_rate_limit_receipt_does_not_claim_release_read(self):
        def execute(args, env, cwd, **kwargs):
            if args[0] == "bad":
                return 1, CLIENTS.ISSUER_ERRORS[0]
            if args[0] == "healthy":
                return 1, ("Error: GET https://api.github.com/repos/helm/chart-releaser/releases/tags/v1.6.0: "
                           "403 API rate limit of 60 still exceeded until 2026-10-24")
            return 0, "version"
        with patch.object(CLIENTS, "prepare_chart_probe", return_value=True), \
             patch.object(CLIENTS, "commands", return_value={"cr": (lambda host: ["bad"], ["healthy"])}), \
             patch.object(CLIENTS, "execute", side_effect=execute), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(CLIENTS.qualify("release", Path("/tmp")))
        result = json.loads(output.getvalue())
        self.assertTrue(result["healthy_tls"])
        self.assertFalse(result["healthy_operation_succeeded"])
        self.assertFalse(result["healthy_authenticated"])

    def test_unexpected_chain_before_or_after_cannot_qualify(self):
        for before, after in [(False, True), (True, False), (True, True)]:
            with patch.object(CLIENTS.chains, "snapshot", side_effect=[{"matches": before}, {"matches": after}]), \
                 patch.object(CLIENTS, "execute", return_value=(1, CLIENTS.ISSUER_ERRORS[0])) as execute:
                result = CLIENTS.negative_probe("git", CLIENTS.HOSTS[0], ["git"], ["expected"], "127.0.0.1", {}, Path("/tmp"))
            self.assertEqual(result["distrusted"], before and after)
            self.assertEqual(execute.call_count, int(before))
            if not before:
                self.assertIsNone(result["chain_after"])

    def test_commands_profiles_and_fixed_hosts(self):
        for profile, expected in [("wincross", {"git"}), ("node", {"git", "gh", "gcloud", "ghtoken", "buf"}),
                                  ("rust", {"git", "gh", "gcloud", "ghtoken", "buf", "cargo-audit"}),
                                  ("release", {"git", "gh", "gcloud", "ghtoken", "cr", "helm", "kubectl", "certifi", "pip"})]:
            commands = CLIENTS.commands(profile, Path("/tmp/probe"))
            self.assertEqual(set(commands), expected)
            for name, (negative, healthy) in commands.items():
                args = negative(CLIENTS.HOSTS[0])
                self.assertTrue(any(CLIENTS.HOSTS[0] in item for item in args))
                self.assertTrue(healthy)
                self.assertNotIn("--insecure", args)
                self.assertNotIn("--push", args)
                self.assertNotIn("--pr", args)
                if name in ("certifi", "pip"):
                    self.assertEqual(args[-2], name)

    def test_python_certifi(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        certifi = types.SimpleNamespace(where=lambda: "/test/certifi.pem")
        with patch.dict(sys.modules, {"certifi": certifi}), \
             patch("ssl.create_default_context") as context, \
             patch("urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(CLIENTS.python_request("certifi", "https://healthy"), 200)
            context.assert_called_once_with(cafile="/test/certifi.pem")
            self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_python_pip(self):
        response = MagicMock(status_code=200)
        requests = MagicMock()
        requests.get.return_value = response
        vendor = types.ModuleType("pip._vendor")
        vendor.requests = requests
        with patch.dict(sys.modules, {"pip._vendor": vendor}):
            self.assertEqual(CLIENTS.python_request("pip", "https://healthy"), 200)
            requests.get.assert_called_once_with("https://healthy", timeout=30)
            response.raise_for_status.assert_called_once()
            response.raise_for_status.side_effect = RuntimeError("failed HTTP request")
            with self.assertRaises(RuntimeError):
                CLIENTS.python_request("pip", "https://healthy")

    def test_qualify_success_and_every_failure(self):
        for failed in [None, "version", "negative", "healthy"]:
            def execute(args, env, cwd, **kwargs):
                if args[0] == "openssl":
                    return 0, ""
                if args[0] == "git":
                    return (1, "missing version") if failed == "version" else (0, "git version test")
                if args[0] == "bad":
                    self.assertTrue(kwargs["stderr_only"])
                    return (0, "accepted") if failed == "negative" else (1, CLIENTS.ISSUER_ERRORS[0])
                return (1, "failure") if failed == "healthy" else (0, "healthy")
            commands = {"git": (lambda host: ["bad", host], ["healthy"]),
                        "pip": (lambda host: ["bad", host], ["healthy"])}
            stream = io.StringIO()
            with patch.object(CLIENTS, "commands", return_value=commands), \
                 patch.object(CLIENTS, "execute", side_effect=execute), contextlib.redirect_stdout(stream):
                self.assertEqual(CLIENTS.qualify("node", Path("/tmp")), failed is None)
            rows = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]["negative"]), 12)
            self.assertFalse(rows[0]["healthy_authenticated"])
            self.assertEqual(rows[0]["healthy_operation_succeeded"], failed != "healthy")

    def test_ghtoken_stdout_diagnostics(self):
        def execute(args, env, cwd, **kwargs):
            if args[0] == "bad":
                self.assertFalse(kwargs["stderr_only"])
                return 1, ("Error: failed listing installations: unable to POST to https://example.test: "
                           + CLIENTS.ISSUER_ERRORS[0])
            if args[0] == "healthy":
                return 1, "Error: failed listing installations: unexpected status code: 404"
            return 0, "version"
        with patch.object(CLIENTS, "commands", return_value={"ghtoken": (lambda host: ["bad"], ["healthy"])}), \
             patch.object(CLIENTS, "execute", side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(CLIENTS.qualify("node", Path("/tmp")))

    def test_synthetic_key_setup_failure_and_wincross_skip(self):
        with patch.object(CLIENTS, "execute", return_value=(1, "openssl failed")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(CLIENTS.qualify("node", Path("/tmp")))
        self.assertEqual(json.loads(output.getvalue())["setup"], "synthetic-app-key")
        with patch.object(CLIENTS, "commands", return_value={}), \
             patch.object(CLIENTS, "execute") as execute:
            self.assertTrue(CLIENTS.qualify("wincross", Path("/tmp")))
            execute.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(CLIENTS, "commands", return_value={}), \
             patch.object(CLIENTS, "execute", return_value=(0, "")):
            self.assertTrue(CLIENTS.qualify("rust", Path(directory)))
            self.assertIn('name = "candidate-probe"', (Path(directory) / "Cargo.lock").read_text())

    def test_chart_fixture_success_and_failures(self):
        for codes in [(0, 0), (1,), (0, 1)]:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(CLIENTS, "execute", side_effect=[(c, "") for c in codes]):
                home = Path(directory)
                self.assertEqual(CLIENTS.prepare_chart_probe(home, {}), codes == (0, 0))
                if codes == (0, 0):
                    with tarfile.open(home / "chart-releaser/packages/candidate-probe-0.1.0.tgz") as archive:
                        self.assertEqual(archive.getnames(), ["candidate-probe/Chart.yaml"])
                        self.assertIn(b"apiVersion: v2", archive.extractfile(archive.getmembers()[0]).read())
        with patch.object(CLIENTS, "prepare_chart_probe", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(CLIENTS.qualify("release", Path("/tmp")))
        self.assertEqual(json.loads(output.getvalue())["setup"], "chart-index-fixture")
        with patch.object(CLIENTS, "prepare_chart_probe", return_value=True), \
             patch.object(CLIENTS, "execute", return_value=(0, "")), \
             patch.object(CLIENTS, "commands", return_value={"cr": (lambda host: ["cr"], ["cr"])}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(CLIENTS.qualify("release", Path("/tmp")))

    def test_smoke_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            for profile in ["node", "rust", "release", "wincross"]:
                for code in [0, 1, None]:
                    with patch.object(CLIENTS, "execute", return_value=(code, "")) as execute, \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(CLIENTS.smoke(profile, Path(directory)), code == 0)
                    args = execute.call_args.args[0]
                    self.assertEqual("--target" in args, profile == "wincross")
                    if profile in ("rust", "wincross"):
                        self.assertIn("fn main", (Path(directory) / "main.rs").read_text())

    def test_main_modes_and_failure_exit(self):
        for status in [200, 404]:
            with patch.object(CLIENTS, "python_request", return_value=status):
                self.assertEqual(CLIENTS.main(["python-request", "pip", "https://healthy"]), int(status != 200))
        for clients, smoke in [(True, True), (True, False), (False, True), (False, False)]:
            with patch.object(CLIENTS, "qualify", return_value=clients), \
                 patch.object(CLIENTS, "smoke", return_value=smoke):
                self.assertEqual(CLIENTS.main(["qualify", "node"]), int(not (clients and smoke)))

    def test_entrypoint_and_parser_reject_unknown_profile(self):
        with patch.object(sys, "argv", ["clients.py", "qualify", "unknown"]), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            runpy.run_path(str(ROOT / "qualification/clients.py"), run_name="__main__")
        self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
