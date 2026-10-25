"""Fail-closed recipe checks; actual package/build/TLS paths run in Linux CI."""
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "images/rust-concourse/cargo-audit"


class CargoAuditBuildTests(unittest.TestCase):
    def test_corrupt_archives_stop_before_extraction_or_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / "bin"
            commands.mkdir()
            # Exercise the real checksum verifier with deliberately invalid bytes.
            (commands / "curl").write_text(
                '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nprintf bad > "$2"\n')
            for command in ("tar", "cargo"):
                (commands / command).write_text(
                    '#!/bin/sh\ntouch "$UNEXPECTED_COMMAND"\nexit 99\n')
            for command in commands.iterdir():
                command.chmod(0o755)
            marker = root / "unexpected"
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                       UNEXPECTED_COMMAND=str(marker))
            result = subprocess.run(["sh", str(RECIPE / "prepare.sh"), str(root / "work")],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FAILED", result.stdout)
            self.assertFalse(marker.exists())

    def test_lock_uses_exactly_one_local_fixed_transport(self):
        packages = tomllib.loads((RECIPE / "Cargo.lock").read_text())["package"]
        transport = [p for p in packages if p["name"] == "gix-transport"]
        self.assertEqual(len(transport), 1)
        self.assertEqual(transport[0]["version"], "0.57.2")
        self.assertNotIn("source", transport[0])
        self.assertNotIn("checksum", transport[0])
        audit = [p for p in packages if p["name"] == "cargo-audit"]
        self.assertEqual([p["version"] for p in audit], ["0.22.2"])
        self.assertNotIn("[[patch.unused]]", (RECIPE / "Cargo.lock").read_text())

    def test_image_scopes_patch_and_installs_only_built_binary(self):
        dockerfile = (RECIPE.parent / "Dockerfile").read_text()
        final = dockerfile.split("FROM trust-base AS final", 1)[1]
        self.assertIn("COPY --from=cargo-audit-build /out/bin/cargo-audit /usr/local/cargo/bin/cargo-audit", final)
        self.assertNotIn("patch.crates-io", final)
        self.assertNotIn("cargo-deny cargo-audit", final)
        self.assertIn("cargo install --locked", dockerfile)
        pipeline = (ROOT / "ci/build/pipeline.yml").read_text()
        self.assertIn("images/rust-concourse/cargo-audit/*", pipeline)


if __name__ == "__main__":
    unittest.main()
