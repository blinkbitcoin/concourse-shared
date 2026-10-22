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

    def test_all_image_builds_use_root_context_and_shared_gate(self):
        pipeline = json.loads(self.render("-o", "json"))
        resources = {r["name"]: r for r in pipeline["resources"]}
        builds = [j for j in pipeline["jobs"] if j["name"].startswith("build-")]
        self.assertEqual(len(builds), 4)
        for job in builds:
            source, build, publish = job["plan"]
            name = source["get"]
            paths = resources[name]["source"]["paths"]
            self.assertIn("images/trust/*", paths)
            dockerfile = paths[0]
            config = build["config"]
            args = config["run"]["args"]
            self.assertIn("--context=" + name, args)
            self.assertIn("--dockerfile=" + dockerfile, args)
            self.assertIn("--cache=false", args)
            self.assertIn("--no-push", args)
            self.assertIn("--digest-file=image/digest", args)
            self.assertEqual(publish["params"]["image"], "image/image.tar")
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
