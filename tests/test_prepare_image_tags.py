"""Exercise the post-Kaniko step without any external tools in PATH."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "images/trust/prepare-image-tags.py"
DIGEST = "sha256:" + "0123456789abcdef" * 4


class PrepareImageTagsTests(unittest.TestCase):
    def run_prepare(self, root):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root / "digest"), str(root / "tags")],
            env={"PATH": "", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
            capture_output=True, text=True,
        )

    def test_valid_digest_without_external_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for suffix in ("", "\n"):
                (root / "digest").write_text(DIGEST + suffix)
                result = self.run_prepare(root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, DIGEST + "\n")
                self.assertEqual((root / "tags").read_text(), DIGEST.replace(":", "-") + "\n")

    def test_missing_or_invalid_digest_never_produces_tags(self):
        invalid = (None, "", "sha256:short", "sha256:" + "g" * 64,
                   "sha256:" + "a" * 63, "sha256:" + "a" * 65,
                   DIGEST.upper(), "sha512:" + "a" * 64, DIGEST + "\nlatest")
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if value is not None:
                    (root / "digest").write_text(value)
                result = self.run_prepare(root)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "tags").exists())
                self.assertEqual(result.stdout, "")

    def test_output_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "digest").write_text(DIGEST)
            (root / "tags").mkdir()
            result = self.run_prepare(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_in_process_entrypoint_and_error_coverage(self):
        spec = importlib.util.spec_from_file_location("prepare_image_tags", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            digest, tags = root / "digest", root / "tags"
            with self.assertRaises(FileNotFoundError):
                module.prepare(digest, tags)
            digest.write_text("invalid")
            with self.assertRaises(ValueError):
                module.prepare(digest, tags)
            digest.write_text(DIGEST)
            tags.mkdir()
            with self.assertRaises(IsADirectoryError):
                module.prepare(digest, tags)
            tags.rmdir()
            with patch.object(sys, "argv", [str(SCRIPT), str(digest), str(tags)]):
                runpy.run_path(str(SCRIPT), run_name="__main__")
            self.assertEqual(tags.read_text(), DIGEST.replace(":", "-") + "\n")

    def test_rendered_task_runs_before_publication(self):
        pipeline = json.loads(subprocess.check_output(
            ["ytt", "-f", "ci/pipeline.yml", "-f", "ci/values.yml", "-o", "json"],
            cwd=ROOT, text=True))
        for job in pipeline["jobs"]:
            if not job["name"].startswith("build-"):
                continue
            source, build, prepare, snapshot, publish, verify = job["plan"]
            with self.subTest(job=job["name"]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_dir = root / source["get"] / "images/trust"
                source_dir.mkdir(parents=True)
                (source_dir / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
                (root / "image").mkdir()
                (root / "image-tags").mkdir()
                (root / "image/digest").write_text(DIGEST)
                result = subprocess.run([sys.executable, *prepare["config"]["run"]["args"]],
                                        cwd=root, env={"PATH": ""}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / publish["params"]["additional_tags"]).read_text(),
                                 DIGEST.replace(":", "-") + "\n")
                self.assertNotIn("on_failure", prepare)
                self.assertNotIn("try", prepare)
                self.assertEqual(build["config"]["run"]["path"], "/kaniko/executor")


if __name__ == "__main__":
    unittest.main()
