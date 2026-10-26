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
        for error in ["certificate has expired", "certificate is expired", "expired certificate", "not yet valid",
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

    def test_cargo_audit_wrapped_distrust_cause(self):
        prefix = ("error: couldn't fetch advisory database: git operation failed: "
                  "An IO error occurred when talking to the server\nCaused by:\n"
                  "  -> error sending request for url (https://example.test/probe.git)\n"
                  "  -> client error (Connect)\n")
        cause = "  -> invalid peer certificate: Other(OtherError(CertDistrusted))\n"
        self.assertTrue(CLIENTS.accepted(1, prefix + cause, negative=True))
        for code, output in [(0, prefix + cause), (None, prefix + cause),
                             (1, prefix), (1, prefix + cause.replace("CertDistrusted", "OtherFailure")),
                             (1, prefix + cause + "certificate has expired"),
                             (1, prefix + cause + "HTTP 403")]:
            with self.subTest(code=code, output=output):
                self.assertFalse(CLIENTS.accepted(code, output, negative=True))

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
        direct = prefix + "403 API rate limit exceeded"
        cached = prefix + ("403 API rate limit of 60 still exceeded until "
                           "2026-10-24 20:40:46 +0000 UTC, not making remote request. [rate reset in 31m59s]")
        self.assertTrue(CLIENTS.accepted(1, direct, auth_control="cr"))
        self.assertFalse(CLIENTS.accepted(1, cached, auth_control="cr"))
        self.assertFalse(CLIENTS.accepted(1, direct, negative=True, auth_control="cr"))
        for output in ["403 Forbidden", "API rate limit exceeded", prefix + "403 Forbidden",
                       prefix.replace("api.github.com", "unrelated.test") + "403 API rate limit exceeded"]:
            self.assertFalse(CLIENTS.accepted(1, output, auth_control="cr"))
        self.assertTrue(CLIENTS.accepted(0, "Index did not change", auth_control="cr"))

    def test_chart_quota_rejects_mixed_diagnostics(self):
        quota = ("Error: GET https://api.github.com/repos/helm/chart-releaser/releases/tags/v1.6.0: "
                 "403 API rate limit exceeded")
        progress = "Loading index file from git repository /tmp/chart/index.yaml\n"
        usage = "\nUsage:\n  cr index [flags]\n"
        self.assertTrue(CLIENTS.accepted(1, progress + quota + usage, auth_control="cr"))
        for failure in ["Error: TLS certificate has expired", "fatal: fetch failed",
                        "TLS certificate has expired", "HTTP 503", "connection refused",
                        "Traceback: exception", "error: unrelated", quota]:
            for mixed in [failure + "\n" + quota, quota + "\n" + failure,
                          quota + " " + failure, progress + quota + usage + failure]:
                with self.subTest(mixed=mixed):
                    self.assertFalse(CLIENTS.accepted(1, mixed, auth_control="cr"))
        for suffix in [" unexpectedly", " until nonsense", " [rate reset in nonsense]"]:
            self.assertFalse(CLIENTS.accepted(1, quota + suffix, auth_control="cr"))

    def test_chart_rate_limit_receipt_does_not_claim_release_read(self):
        def execute(args, env, cwd, **kwargs):
            if args[0] == "bad":
                return 1, CLIENTS.ISSUER_ERRORS[0]
            if args[0] == "healthy":
                return 1, ("Error: GET https://api.github.com/repos/helm/chart-releaser/releases/tags/v1.6.0: "
                           "403 API rate limit " + variant + extra)
            return 0, "version"
        for variant, extra, passed in [
                ("exceeded", "", True), ("exceeded", "\nError: TLS certificate has expired", False),
                ("of 60 still exceeded until 2026-10-24 20:40:46 +0000 UTC, "
                 "not making remote request. [rate reset in 31m59s]", "", False)]:
            with patch.object(CLIENTS, "prepare_chart_probe", return_value=True), \
                 patch.object(CLIENTS, "commands", return_value={"cr": (lambda host: ["bad"], ["healthy"])}), \
                 patch.object(CLIENTS, "execute", side_effect=execute), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(CLIENTS.qualify("release", Path("/tmp")), passed)
            result = json.loads(output.getvalue())
            self.assertEqual(result["healthy_tls"], passed)
            self.assertEqual(result["passed"], passed)
            self.assertFalse(result["healthy_operation_succeeded"])
            self.assertFalse(result["healthy_authenticated"])

    def test_chart_negative_receipt_retains_full_classification_input(self):
        issuer = "Error: Get https://example.test/: tls: failed to verify certificate: x509: certificate signed by unknown authority\n"
        usage = "Usage:\n  cr index [flags]\n" + "      --config string   Configuration file\n" * 40
        for rejected in ("", "Error: certificate has expired\n", "Error: HTTP 403\n"):
            diagnostic = issuer + usage + rejected + usage
            def execute(args, env, cwd, **kwargs):
                return (1, diagnostic) if args[0] == "negative" else (0, "ok")
            with patch.object(CLIENTS, "prepare_chart_probe", return_value=True), \
                 patch.object(CLIENTS, "commands", return_value={"cr": (lambda host: ["negative"], ["healthy"])}), \
                 patch.object(CLIENTS, "execute", side_effect=execute), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(CLIENTS.qualify("release", Path("/tmp")), not rejected)
            receipt = json.loads(output.getvalue())
            self.assertEqual(len(receipt["negative"]), 12)
            for probe in receipt["negative"]:
                self.assertEqual(probe["diagnostic"], diagnostic)
                self.assertEqual(probe["distrusted"], not rejected)
                self.assertEqual(CLIENTS.accepted(probe["exit"], probe["diagnostic"],
                                                 negative=True, auth_control="cr"), probe["distrusted"])

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
                self.assertIn(name, CLIENTS.VERSION_COMMANDS)
                if name == "certifi":
                    self.assertEqual(args[-2], "certifi-request")
                if name == "pip":
                    self.assertEqual(args[:4], [sys.executable, "-m", "pip", "download"])
                    for option in ("--no-cache-dir", "--no-deps", "--only-binary=:all:", "--disable-pip-version-check"):
                        self.assertIn(option, args)
                    self.assertEqual(args[args.index("--retries") + 1], "0")
                    self.assertEqual(args[args.index("--timeout") + 1], "10")
                    self.assertIn("pip==26.2.2", healthy)
                    self.assertNotEqual(args[args.index("--dest") + 1], healthy[healthy.index("--dest") + 1])
                if name == "helm":
                    self.assertNotEqual(args[3], negative(CLIENTS.HOSTS[1])[3])

    def test_python_certifi(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        certifi = types.SimpleNamespace(where=lambda: "/test/certifi.pem")
        with patch.dict(sys.modules, {"certifi": certifi}), \
             patch("ssl.create_default_context") as context, \
             patch("urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(CLIENTS.certifi_request("https://healthy"), 200)
            context.assert_called_once_with(cafile="/test/certifi.pem")
            self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_qualify_success_and_every_failure(self):
        for failed in [None, "version", "negative", "healthy"]:
            def execute(args, env, cwd, **kwargs):
                if args[0] == "openssl":
                    return 0, ""
                if args[0] == "git":
                    return (1, "missing version") if failed == "version" else (0, "git version test")
                if args[0] == "bad":
                    self.assertEqual(kwargs["stderr_only"], args[1] == "git")
                    return (0, "accepted") if failed == "negative" else (1, CLIENTS.ISSUER_ERRORS[0])
                return (1, "failure") if failed == "healthy" else (0, "healthy")
            commands = {"git": (lambda host: ["bad", "git", host], ["healthy"]),
                        "pip": (lambda host: ["bad", "pip", host], ["healthy"])}
            stream = io.StringIO()
            with patch.object(CLIENTS, "commands", return_value=commands), \
                 patch.object(CLIENTS, "execute", side_effect=execute), contextlib.redirect_stdout(stream):
                self.assertEqual(CLIENTS.qualify("node", Path("/tmp")), failed is None)
            rows = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]["negative"]), 12)
            self.assertFalse(rows[0]["healthy_authenticated"])
            self.assertEqual(rows[0]["healthy_operation_succeeded"], failed != "healthy")

    def test_pip_stdout_certificate_error_and_stderr_resolution_failure(self):
        stderr = ("ERROR: Could not find a version that satisfies the requirement candidate-probe==0 "
                  "(from versions: none)\nERROR: No matching distribution found for candidate-probe==0\n")
        prefix = "Could not fetch URL https://example.test/simple/candidate-probe/: "
        issuer = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"
        for stdout, passed in [(prefix + issuer + " - skipping\n", True), ("", False),
                               (prefix + issuer + " certificate has expired", False),
                               (prefix + "HTTP 403: " + issuer, False)]:
            result = types.SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)
            with patch.object(CLIENTS.subprocess, "run", return_value=result):
                row = CLIENTS.negative_probe("pip", "example.test", ["python", "-m", "pip", "download"],
                                             ["expected"], "127.0.0.1", {}, Path("/tmp"))
            self.assertEqual(row["distrusted"], passed)
            self.assertIn("No matching distribution", row["diagnostic"])
            if passed:
                self.assertIn("unable to get local issuer certificate", row["diagnostic"])

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
                    if profile == "release":
                        self.assertEqual(args[0], sys.executable)
                    if profile in ("rust", "wincross"):
                        self.assertIn("fn main", (Path(directory) / "main.rs").read_text())

    def test_preflight_failure_aborts_before_clients(self):
        with patch.object(CLIENTS.chains, "snapshot", return_value={"matches": False, "reason": "expired"}) as snapshot, \
             patch.object(CLIENTS, "execute") as execute, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(CLIENTS.qualify("release", Path("/tmp")))
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(snapshot.call_args.kwargs["minimum_validity"], 60 * 86400)
        execute.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["setup"], "chain-preflight")
        self.assertEqual(receipt["host"], CLIENTS.HOSTS[0])
        self.assertFalse(receipt["passed"])

    def test_setup_errors_are_structured_and_main_still_smokes(self):
        for function, error in [("published_chains", ValueError("checksum")),
                                ("published_chains", CLIENTS.zipfile.BadZipFile("bad")),
                                ("published_chains", KeyError("chain")),
                                ("pinned_peers", OSError("hosts"))]:
            with patch.object(CLIENTS.chains, function, side_effect=error), \
                 patch.object(CLIENTS, "smoke", return_value=True) as smoke, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(CLIENTS.main(["qualify", "node"]), 1)
            self.assertEqual(json.loads(output.getvalue())["setup"], "qualification-inputs")
            smoke.assert_called_once()

    def test_deadline_and_remaining_subprocess_budget(self):
        def setup(args, env, cwd, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            return 0, "ok"
        with patch.object(CLIENTS.time, "monotonic", side_effect=[0, CLIENTS.QUALIFICATION_TIMEOUT - 5, CLIENTS.QUALIFICATION_TIMEOUT - 3]), \
             patch.object(CLIENTS, "commands", return_value={}), \
             patch.object(CLIENTS, "execute", side_effect=setup):
            self.assertTrue(CLIENTS.qualify("node", Path("/tmp")))
        for elapsed in [CLIENTS.QUALIFICATION_TIMEOUT, CLIENTS.QUALIFICATION_TIMEOUT + 1]:
            with patch.object(CLIENTS.time, "monotonic", side_effect=[0, elapsed]), \
                 patch.object(CLIENTS, "execute") as execute, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertFalse(CLIENTS.qualify("node", Path("/tmp")))
            execute.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["aborted"], "qualification-deadline")

    def test_deadline_reached_during_last_command_is_structured(self):
        with patch.object(CLIENTS.time, "monotonic", side_effect=[0, 1, CLIENTS.QUALIFICATION_TIMEOUT]), \
             patch.object(CLIENTS, "execute", return_value=(None, "TimeoutExpired")) as execute, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(CLIENTS.qualify("node", Path("/tmp")))
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(json.loads(output.getvalue())["aborted"], "qualification-deadline")

    def test_chain_drift_aborts_with_probe_receipt(self):
        for before, after in [(False, True), (True, False)]:
            snapshots = [{"matches": True}] * len(CLIENTS.HOSTS) + [{"matches": before}, {"matches": after}]
            with patch.object(CLIENTS.chains, "snapshot", side_effect=snapshots), \
                 patch.object(CLIENTS, "execute", return_value=(1, CLIENTS.ISSUER_ERRORS[0])), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertFalse(CLIENTS.qualify("wincross", Path("/tmp")))
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["aborted"], "chain-drift")
            self.assertEqual(receipt["probe"]["host"], CLIENTS.HOSTS[0])

    def test_main_modes_and_failure_exit(self):
        for status in [200, 404]:
            with patch.object(CLIENTS, "certifi_request", return_value=status):
                self.assertEqual(CLIENTS.main(["certifi-request", "https://healthy"]), int(status != 200))
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
