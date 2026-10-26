import base64
import hashlib
import datetime
import io
from pathlib import Path
import re
import runpy
import tempfile
import os
import subprocess
import shutil
import yaml
import unittest
from unittest.mock import patch
import zipfile

from qualification import chains


class ChainBindingTests(unittest.TestCase):
    def test_fingerprints_and_invalid_pem(self):
        pem = "-----BEGIN CERTIFICATE-----\nY2VydA==\n-----END CERTIFICATE-----"
        self.assertEqual(chains.fingerprints(pem), [hashlib.sha256(b"cert").hexdigest()])
        self.assertEqual(chains.fingerprints("no certificates"), [])
        with self.assertRaises(ValueError):
            chains.fingerprints(pem.replace("Y2VydA==", "?"))

    def test_published_archive_and_failures(self):
        expected = chains.published_chains()
        self.assertEqual(set(expected), set(chains.HOSTS))
        self.assertTrue(all(len(c) >= 2 for c in expected.values()))
        with patch.object(chains, "FIXTURE_SHA256", "wrong"), self.assertRaisesRegex(ValueError, "checksum"):
            chains.published_chains()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for host in chains.HOSTS:
                archive.writestr(f"chains/{host}.pem", "no certificates")
        payload = stream.getvalue()
        with patch.object(chains, "FIXTURE_ZIP", base64.b64encode(payload).decode()), \
             patch.object(chains, "FIXTURE_SHA256", hashlib.sha256(payload).hexdigest()), \
             self.assertRaisesRegex(ValueError, "Incomplete"):
            chains.published_chains()

    def test_pinned_hosts_success_missing_duplicate_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts"
            lines = "127.0.0.1 localhost\n# ignored\n" + "\n".join(f"192.0.2.1 {h} # comment" for h in chains.HOSTS)
            path.write_text(lines)
            self.assertEqual(chains.pinned_peers(path), {h: "192.0.2.1" for h in chains.HOSTS})
            for text in ["127.0.0.1 localhost", lines + "\n192.0.2.2 " + chains.HOSTS[0],
                         lines.replace("192.0.2.1", "not-an-address")]:
                path.write_text(text)
                with self.assertRaises(ValueError):
                    chains.pinned_peers(path)

    def test_snapshot_identity_validity_and_warning_failures(self):
        pem = ("-----BEGIN CERTIFICATE-----Y2VydA==-----END CERTIFICATE-----\n"
               "-----BEGIN CERTIFICATE-----cm9vdA==-----END CERTIFICATE-----")
        expected = chains.fingerprints(pem)
        cases = [(0, pem, 0, 0, 0, True), (0, pem, 0, 0, 60, True),
                 (0, pem, 1, 0, 0, False), (0, pem, None, 0, 0, False),
                 (0, pem, 0, 1, 60, False), (0, pem, 0, None, 60, False),
                 (0, pem.replace("Y2VydA==", "b3RoZXI="), 0, 0, 0, False),
                 (0, "", 0, 0, 0, False), (1, pem, 0, 0, 0, False),
                 (None, "TimeoutExpired", 0, 0, 0, False),
                 (0, pem.replace("Y2VydA==", "?"), 0, 0, 0, False)]
        for code, output, valid, future, warning, match in cases:
            calls = []
            def execute(args, *unused):
                calls.append(args)
                if args[1] == "s_client":
                    return code, output
                self.assertEqual(Path(args[args.index("-trusted") + 1]).read_text(), pem.split("\n")[1])
                self.assertIn("-verify_hostname", args)
                self.assertIn("-no-CAstore", args)
                return (future if "-attime" in args else valid), "verification diagnostic"
            with tempfile.TemporaryDirectory() as directory, patch.object(chains.time, "time", return_value=1000):
                result = chains.snapshot(chains.HOSTS[0], "192.0.2.1", expected, execute, {}, Path(directory),
                                         minimum_validity=warning)
            self.assertEqual(result["matches"], match)
            self.assertIn("192.0.2.1:443", calls[0])
            if warning and valid == 0:
                self.assertIn("1060", calls[-1])
        single = pem.split("\n")[0]
        result = chains.snapshot("host", "peer", chains.fingerprints(single),
                                 lambda *args: (0, single), {}, Path("/tmp"))
        self.assertFalse(result["matches"])

    def test_real_openssl_transcript_and_chain_validation(self):
        transcript = (Path(__file__).parent / "fixtures/candidate-s-client.txt").read_text()
        host = "dv-g3b-emudhra.we-have-a-problem.com"
        expected = chains.published_chains()[host]
        self.assertEqual(chains.fingerprints(transcript), expected)
        self.assertIn("Server certificate", transcript)
        noise = "\nPost-Handshake New Session Ticket arrived:\nSSL-Session:\n    Protocol : TLSv1.3\n"
        self.assertEqual(chains.fingerprints(transcript + noise), expected)
        openssl = shutil.which("openssl", path="/opt/homebrew/opt/openssl@3/bin:" + os.environ["PATH"])
        from qualification import clients
        # Use real certificate verification at deterministic valid/expired times.
        # This does not contact a network endpoint or modify any trust store.
        for at, valid in [("1792699200", True), ("1893456000", False), ("946684800", False)]:
            for diagnostic in ["server certificate verification failed. CAfile: test CRLfile: none",
                               "The certificate is NOT trusted. The certificate chain uses expired certificate."]:
                calls = []
                def execute(args, env, cwd, **kwargs):
                    calls.append(args)
                    if args[0] == "git":
                        return 1, diagnostic
                    if args[1] == "s_client":
                        return 0, transcript
                    command = [openssl] + args[1:-1] + ["-attime", at, args[-1]]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                    return result.returncode, result.stdout + result.stderr
                with tempfile.TemporaryDirectory() as directory:
                    row = clients.negative_probe("git", host, ["git"], expected, "192.0.2.1", {},
                                                 Path(directory), execute)
                self.assertEqual(row["chain_before"]["valid_now"], valid)
                self.assertEqual(row["distrusted"], valid and "expired" not in diagnostic)
                self.assertEqual(any(c[0] == "git" for c in calls), valid)

    def test_real_chain_expiry_warning_boundary(self):
        transcript = (Path(__file__).parent / "fixtures/candidate-s-client.txt").read_text()
        host = "dv-g3b-emudhra.we-have-a-problem.com"
        expected = chains.published_chains()[host]
        openssl = shutil.which("openssl", path="/opt/homebrew/opt/openssl@3/bin:" + os.environ["PATH"])
        leaf = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", transcript, re.S)[0]
        end = subprocess.run([openssl, "x509", "-noout", "-enddate"], input=leaf,
                             text=True, capture_output=True, check=True).stdout.strip().split("=", 1)[1]
        expiry = datetime.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        def execute(args, env, cwd):
            if args[1] == "s_client":
                return 0, transcript
            fixed_time = [] if "-attime" in args else ["-attime", "1792699200"]
            result = subprocess.run([openssl] + args[1:-1] + fixed_time + [args[-1]],
                                    capture_output=True, text=True, timeout=10)
            return result.returncode, result.stdout + result.stderr
        for offset, good in [(-1, True), (1, False)]:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(chains.time, "time", return_value=expiry - 60 * 86400 + offset):
                result = chains.snapshot(host, "192.0.2.1", expected, execute, {}, Path(directory),
                                         minimum_validity=60 * 86400)
            self.assertTrue(result["valid_now"])
            self.assertEqual(result["matches"], good)
            if not good:
                self.assertEqual(result["reason"], "chain-expires-within-warning-window")

    def test_docker_arguments_and_entrypoint(self):
        with patch.object(chains.socket, "gethostbyname", return_value="192.0.2.1"):
            self.assertEqual(chains.docker_arguments(), [f"--add-host={h}:192.0.2.1" for h in chains.HOSTS])
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                runpy.run_path(str(Path(chains.__file__)), run_name="__main__")
            self.assertEqual(len(output.getvalue().splitlines()), len(chains.HOSTS))

    def test_workflow_pins_and_inputs_stay_consistent(self):
        root = Path(__file__).resolve().parents[1]
        matrix = yaml.safe_load((root / ".github/workflows/candidate-qualification.yml").read_text())
        canary = yaml.safe_load((root / ".github/workflows/node-consumer-canary.yml").read_text())
        rows = matrix["jobs"]["candidate"]["strategy"]["matrix"]["include"]
        expected = {"node": ("nodejs-concourse", "--node"), "rust": ("rust-concourse", "--rustup"),
                    "release": ("release-pipeline", ""), "wincross": ("wincross-rust", "--rustup")}
        self.assertEqual({r["profile"] for r in rows}, set(expected))
        self.assertEqual(len(rows), len(expected))
        docs = (root / "qualification/README.md").read_text()
        for row in rows:
            self.assertEqual((row["repository"], row["verifier"]), expected[row["profile"]])
            self.assertRegex(row["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertIn(f'| {row["profile"]} | {row["repository"]} | {row["digest"]} |', docs)
        node = next(r for r in rows if r["profile"] == "node")
        self.assertEqual(canary["jobs"]["blink-client"]["env"]["IMAGE"],
                         "us.gcr.io/galoy-org/nodejs-concourse@" + node["digest"])
        self.assertIn("images/trust/**", matrix["on"]["pull_request"]["paths"])
        self.assertIn("tests/fixtures/candidate-*", matrix["on"]["pull_request"]["paths"])
        self.assertIn(".github/workflows/node-consumer-canary.yml", matrix["on"]["pull_request"]["paths"])
        self.assertIn(".github/workflows/candidate-qualification.yml", canary["on"]["pull_request"]["paths"])
        self.assertEqual(matrix["on"]["schedule"], [{"cron": "43 7 * * 1"}])
        self.assertIn("workflow_dispatch", matrix["on"])
        self.assertEqual(matrix["jobs"]["candidate"]["needs"], "contracts")
        contract = "\n".join(s.get("run", "") for s in matrix["jobs"]["contracts"]["steps"])
        self.assertIn("coverage run --branch --source=qualification", contract)
        self.assertIn("coverage report --fail-under=100", contract)
        trust = yaml.safe_load((root / ".github/workflows/trust-test.yml").read_text())
        trust_runs = [s.get("run", "") for s in trust["jobs"]["trust"]["steps"]]
        self.assertIn("python3 -m pip install -r qualification/requirements-test.txt", trust_runs)
        steps = matrix["jobs"]["candidate"]["steps"]
        self.assertIn('"${pinned_hosts[@]}"', "\n".join(s.get("run", "") for s in steps))


if __name__ == "__main__":
    unittest.main()
