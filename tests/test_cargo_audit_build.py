"""Fail-closed recipe checks; actual package/build/TLS paths run in Linux CI."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import runpy
import sys
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "images/rust-concourse/cargo-audit"

SPEC = importlib.util.spec_from_file_location("cargo_audit_lock", RECIPE / "verify-lock.py")
LOCK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCK)


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
        stage = dockerfile.split("FROM trust-base AS cargo-audit-build", 1)[1].split("FROM trust-base AS final", 1)[0]
        self.assertIn("cargo install --locked", stage)
        self.assertIn("--path /build/source/cargo-audit-0.22.2", stage)
        self.assertLess(stage.index("python3 /build/recipe/verify-lock.py"),
                        stage.index("&& cp /build/recipe/Cargo.lock"))
        self.assertIn("/build/source/cargo-audit-0.22.2/Cargo.lock /build/recipe/Cargo.lock", stage)
        pipeline = (ROOT / "ci/build/pipeline.yml").read_text()
        self.assertIn("images/rust-concourse/cargo-audit/*", pipeline)


class CargoAuditLockTests(unittest.TestCase):
    def setUp(self):
        self.proposed = tomllib.loads((RECIPE / "Cargo.lock").read_text())
        self.delta = json.loads((RECIPE / "lock-delta.json").read_text())
        # Reconstruct the release snapshot offline. The independently recorded
        # release hash anchors every unchanged package, not just the allowlist.
        self.baseline = copy.deepcopy(self.proposed)
        for package in self.delta["add"]:
            self.baseline["package"].remove(package)
        self.baseline["package"].extend(self.delta["remove"])

    def test_checked_in_lock_matches_published_snapshot_and_exact_delta(self):
        self.assertEqual(hashlib.sha256(LOCK.canonical(self.baseline)).hexdigest(),
                         self.delta["baseline_sha256"])
        LOCK.verify(self.baseline, self.proposed, self.delta)
        reordered = copy.deepcopy(self.proposed)
        reordered["package"].reverse()
        LOCK.verify(self.baseline, reordered, self.delta)

    def test_rejects_unrelated_and_allowlisted_package_drift(self):
        for name in ("cargo-lock", "reqwest"):
            for field, value in [("version", "99.0.0"), ("source", "git+https://example.test"),
                                 ("checksum", "0" * 64), ("dependencies", ["unexpected"])]:
                with self.subTest(name=name, field=field):
                    changed = copy.deepcopy(self.proposed)
                    package = next(p for p in changed["package"] if p["name"] == name)
                    package[field] = value
                    with self.assertRaisesRegex(ValueError, "reviewed transport delta"):
                        LOCK.verify(self.baseline, changed, self.delta)

    def test_rejects_added_removed_duplicate_packages_and_metadata_drift(self):
        extra = copy.deepcopy(self.proposed)
        extra["package"].append({"name": "unexpected", "version": "1.0.0"})
        missing = copy.deepcopy(self.proposed)
        missing["package"].pop()
        duplicate = copy.deepcopy(self.proposed)
        duplicate["package"].append(copy.deepcopy(duplicate["package"][0]))
        version = copy.deepcopy(self.proposed)
        version["version"] = 3
        metadata = copy.deepcopy(self.proposed)
        metadata["patch"] = {"unused": [{"name": "gix-transport"}]}
        for changed in (extra, missing, duplicate, version, metadata):
            with self.assertRaisesRegex(ValueError, "reviewed transport delta"):
                LOCK.verify(self.baseline, changed, self.delta)

    def test_rejects_wrong_baseline_and_invalid_delta(self):
        baseline = copy.deepcopy(self.baseline)
        baseline["package"].pop()
        with self.assertRaisesRegex(ValueError, "baseline"):
            LOCK.verify(baseline, self.proposed, self.delta)
        delta = copy.deepcopy(self.delta)
        delta["remove"].append({"name": "absent", "version": "1.0.0"})
        with self.assertRaises(ValueError):
            LOCK.verify(self.baseline, self.proposed, delta)

    def test_cli_compares_files_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "Cargo.lock.upstream"
            # Lock records contain only strings/string arrays; JSON values are
            # valid TOML values here. Do not rely on a network fetch in unit tests.
            text = "version = 4\n"
            for package in self.baseline["package"]:
                text += "\n[[package]]\n" + "".join(
                    f"{key} = {json.dumps(value)}\n" for key, value in package.items())
            baseline.write_text(text)
            argv = [str(RECIPE / "verify-lock.py"), str(baseline), str(RECIPE / "Cargo.lock")]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as output:
                runpy.run_path(argv[0], run_name="__main__")
            self.assertIn("preserves the published snapshot", output.getvalue())
            baseline.write_text(text.replace("version = 4", "version = 3", 1))
            with patch.object(sys, "argv", argv), self.assertRaisesRegex(ValueError, "baseline"):
                runpy.run_path(argv[0], run_name="__main__")


if __name__ == "__main__":
    unittest.main()
