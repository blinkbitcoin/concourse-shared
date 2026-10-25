import base64
import hashlib
import io
from pathlib import Path
import re
import runpy
import tempfile
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

    def test_snapshot_success_mismatch_transport_and_parse_failure(self):
        pem = "-----BEGIN CERTIFICATE-----Y2VydA==-----END CERTIFICATE-----"
        expected = chains.fingerprints(pem)
        for code, output, match in [(0, pem, True), (0, pem.replace("Y2VydA==", "b3RoZXI="), False),
                                     (0, "", False), (1, pem, False),
                                     (None, "TimeoutExpired", False), (0, pem.replace("Y2VydA==", "?"), False)]:
            calls = []
            def execute(*args):
                calls.append(args)
                return code, output
            result = chains.snapshot(chains.HOSTS[0], "192.0.2.1", expected, execute, {}, Path("/tmp"))
            self.assertEqual(result["matches"], match)
            self.assertIn("192.0.2.1:443", calls[0][0])
            self.assertIn(chains.HOSTS[0], calls[0][0])

    def test_docker_arguments_and_entrypoint(self):
        with patch.object(chains.socket, "gethostbyname", return_value="192.0.2.1"):
            self.assertEqual(chains.docker_arguments(), [f"--add-host={h}:192.0.2.1" for h in chains.HOSTS])
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                runpy.run_path(str(Path(chains.__file__)), run_name="__main__")
            self.assertEqual(len(output.getvalue().splitlines()), len(chains.HOSTS))

    def test_workflow_pins_and_inputs_stay_consistent(self):
        root = Path(__file__).resolve().parents[1]
        matrix = (root / ".github/workflows/candidate-qualification.yml").read_text()
        canary = (root / ".github/workflows/node-consumer-canary.yml").read_text()
        digest = re.search(r"repository: nodejs-concourse\s+digest: (sha256:[0-9a-f]{64})", matrix)[1]
        self.assertIn("nodejs-concourse@" + digest, canary)
        self.assertIn("'images/trust/**'", matrix)
        self.assertIn("'.github/workflows/node-consumer-canary.yml'", matrix)
        self.assertIn("'.github/workflows/candidate-qualification.yml'", canary)
        self.assertIn('"${pinned_hosts[@]}"', matrix)


if __name__ == "__main__":
    unittest.main()
