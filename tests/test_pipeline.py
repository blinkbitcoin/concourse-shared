"""Check rendered image-build inputs without connecting to Concourse."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def render(self, *args):
        return subprocess.check_output(
            ["ytt", "-f", "ci/pipeline.yml", "-f", "ci/values.yml", *args],
            cwd=ROOT, text=True,
        )

    def test_generation_is_current_and_single_document(self):
        self.assertEqual(self.render(), (ROOT / "ci/build/pipeline.yml").read_text())
        pipeline = json.loads(self.render("-o", "json"))
        self.assertIsInstance(pipeline, dict)
        self.assertIn("jobs", pipeline)

    def test_consumer_automation_requires_explicit_opt_in(self):
        for enabled in (False, True):
            pipeline = json.loads(self.render("--data-value-yaml", f"enable_shared_file_updates={str(enabled).lower()}", "-o", "json"))
            jobs = [j for j in pipeline["jobs"] if j["name"].startswith("bump-shared-files-")]
            self.assertEqual(bool(jobs), enabled)

    def test_all_image_builds_use_root_context_and_shared_gate(self):
        pipeline = json.loads(self.render("-o", "json"))
        resources = {r["name"]: r for r in pipeline["resources"]}
        builds = [j for j in pipeline["jobs"] if j["name"].startswith("build-")]
        self.assertEqual(len(builds), 4)
        for job in builds:
            source, build, snapshot, publish, verify = job["plan"]
            name = source["get"]
            paths = resources[name]["source"]["paths"]
            self.assertIn("images/trust/*", paths)
            dockerfile = paths[0]
            config = build["config"]
            self.assertNotIn("digest", config["image_resource"]["source"])
            self.assertEqual(config["image_resource"]["source"]["tag"], "v1.24.0-debug")
            self.assertEqual(config["image_resource"]["version"]["digest"], "sha256:2562c4fe551399514277ffff7dcca9a3b1628c4ea38cb017d7286dc6ea52f4cd")
            self.assertEqual(config["run"]["path"], "/busybox/sh")
            args = config["run"]["args"][-1].split()
            self.assertIn("--context=" + name, args)
            self.assertIn("--dockerfile=" + dockerfile, args)
            self.assertIn("--cache=false", args)
            self.assertIn("--no-push", args)
            self.assertIn("--digest-file=image/digest", args)
            self.assertEqual(publish["params"]["image"], "image/image.tar")
            self.assertEqual(publish["params"]["additional_tags"], "image/tags")
            self.assertEqual(resources[publish["put"]]["source"]["tag"], "trust-candidate")
            self.assertIn("sha256-%s", config["run"]["args"][-1])
            self.assertEqual(snapshot["config"]["run"]["args"][1], "snapshot")
            self.assertEqual(verify["config"]["run"]["args"][1], "verify")
            self.assertEqual(verify["config"]["run"]["args"][-3:], ["image/digest", publish["put"] + "/digest", "previous/digest.json"])
            self.assertEqual(publish["get_params"], {"skip_download": True})
            source = (ROOT / dockerfile).read_text()
            self.assertIn("@sha256:", source.splitlines()[0])
            self.assertLess(source.index("verify.py refresh"), source.index("FROM trust-base AS final"))
            self.assertTrue(source.strip().splitlines()[-1].startswith(
                "RUN /usr/bin/python3 /usr/local/lib/ci-trust/verify.py verify"))
            if "nodejs" in name:
                self.assertIn("verify.py refresh --node", source)
                self.assertIn("verify.py verify --node", source)


if __name__ == "__main__":
    unittest.main()
