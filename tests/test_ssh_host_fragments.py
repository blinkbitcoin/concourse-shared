"""Render the ZRH1 Docker-host fragments the way a consumer repository does."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = ROOT / "shared/ci/pipeline-fragments.lib.yml"

CONSUMER = '''#@ load("@ytt:data", "data")
#@ load("vendor/pipeline-fragments.lib.yml", "zrh1_docker_host_pool", "run_on_zrh1_nix_host",
#@   "test_on_zrh1_docker_host", "docker_host_pool", "run_on_nix_host", "test_on_docker_host")
jobs:
- #@ run_on_zrh1_nix_host("integration-tests", "make test-in-ci", {"RAIN_API_KEY": "((rain.key))"})
- #@ run_on_zrh1_nix_host("e2e-tests", "make e2e", workload_timeout_minutes=40)
- #@ test_on_zrh1_docker_host("compose-tests")
- #@ run_on_nix_host("legacy-nix", "make legacy")
- #@ test_on_docker_host("legacy-compose")
resources:
- #@ zrh1_docker_host_pool()
- #@ docker_host_pool()
'''

VALUES = '''#@data/values
---
gh_repository: fixture
github_private_key: KEY
slack_channel: chan
'''


@unittest.skipUnless(shutil.which("ytt") or os.environ.get("GITHUB_ACTIONS") == "true", "requires ytt")
class Fragments(unittest.TestCase):
    def render(self, pipeline=CONSUMER):
        with tempfile.TemporaryDirectory() as tmp:
            ci = Path(tmp)
            (ci / "vendor").mkdir()
            shutil.copyfile(FRAGMENTS, ci / "vendor/pipeline-fragments.lib.yml")
            (ci / "pipeline.yml").write_text(pipeline)
            (ci / "values.yml").write_text(VALUES)
            result = subprocess.run(["ytt", "-f", str(ci), "-o", "json"], capture_output=True, text=True)
            return result

    def pipeline(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        return {j["name"]: j for j in rendered["jobs"]}, {r["name"]: r for r in rendered["resources"]}

    def test_pool_targets_zrh1_hosts_only(self):
        _, resources = self.pipeline()
        self.assertEqual(resources["zrh1-docker-host"]["source"]["pool"], "zrh1-docker-hosts")
        self.assertEqual(resources["zrh1-docker-host"]["source"]["uri"], "git@github.com:blinkbitcoin/concourse-locks.git")
        self.assertEqual(resources["docker-host"]["source"]["pool"], "docker-hosts")

    def test_zrh1_jobs_run_once_untagged_and_release_only_after_cleanup_receipt(self):
        jobs, _ = self.pipeline()
        expected = {"integration-tests": ("nix", 12, "nodejs-concourse"), "e2e-tests": ("nix", 40, "nodejs-concourse"),
                    "compose-tests": ("compose", 20, "rust-concourse")}
        for name, (mode, minutes, image) in expected.items():
            with self.subTest(job=name):
                job = jobs[name]
                # Inputs are fetched before the claim, so a failed get cannot hold it.
                gets, acquire, task = job["plan"]
                self.assertEqual(gets, {"in_parallel": [{"get": "repo", "trigger": True}, {"get": "pipeline-tasks"}]})
                self.assertEqual(acquire, {"put": "zrh1-docker-host", "params": {"acquire": True}})
                self.assertEqual(task["attempts"], 1)
                self.assertNotIn("tags", task)
                self.assertEqual(task["timeout"], f"{minutes + 25}m")
                config = task["config"]
                self.assertTrue(config["image_resource"]["source"]["repository"].endswith("/" + image))
                self.assertEqual(config["run"]["path"], "pipeline-tasks/ci/vendor/tasks/ssh-host-run.sh")
                self.assertEqual(config["outputs"], [{"name": "host-cleanup"}])
                self.assertEqual(config["inputs"][:2], [{"name": "pipeline-tasks"}, {"name": "zrh1-docker-host"}])
                params = config["params"]
                self.assertEqual(config["inputs"][2], {"name": "repo", "path": params["REPO_PATH"]})
                self.assertEqual(params["REPO_PATH"], "fixture-" + name)
                self.assertEqual(params["SSH_HOST_MODE"], mode)
                self.assertEqual(params["WORKLOAD_TIMEOUT_MINUTES"], str(minutes))
                self.assertEqual(params["SSH_PRIVATE_KEY"], "((staging-ssh.ssh_private_key))")
                self.assertEqual(params["GOOGLE_CREDENTIALS"], "((staging-gcp-creds.creds_json))")
                self.assertNotIn("SSH_PUB_KEY", params)
                gate, release = task["ensure"]["do"]
                self.assertEqual(gate["config"]["run"]["args"],
                                 ["pipeline-tasks/ci/vendor/tasks/ssh-host-check-cleanup.sh"])
                self.assertIn({"name": "host-cleanup", "optional": True}, gate["config"]["inputs"])
                self.assertIn({"name": "zrh1-docker-host"}, gate["config"]["inputs"])
                self.assertEqual(release, {"put": "zrh1-docker-host", "params": {"release": "zrh1-docker-host"}})
        self.assertEqual(jobs["integration-tests"]["plan"][2]["config"]["params"]["PARAMS_KEYS"], "RAIN_API_KEY")
        self.assertEqual(jobs["integration-tests"]["plan"][2]["config"]["params"]["CMD"], "make test-in-ci")
        self.assertEqual(jobs["compose-tests"]["plan"][2]["config"]["params"]["TEST_CONTAINER"], "compose-tests")
        self.assertIn("on_failure", jobs["compose-tests"])
        self.assertNotIn("on_failure", jobs["integration-tests"])

    def test_legacy_gcp_fragments_are_unchanged(self):
        jobs, _ = self.pipeline()
        for name, path in [("legacy-nix", "run-on-nix-host.sh"), ("legacy-compose", "test-on-docker-host.sh")]:
            task = jobs[name]["plan"][2]
            self.assertEqual(task["tags"], ["galoy-staging"])
            self.assertEqual(task["attempts"], 2)
            self.assertEqual(task["config"]["run"]["path"], "pipeline-tasks/ci/vendor/tasks/" + path)
            self.assertEqual(task["ensure"], {"put": "docker-host", "params": {"release": "docker-host"}})

    def test_reserved_or_invalid_additional_params_fail_render(self):
        # ssh-host-run.sh refuses these too; failing at render keeps hosts unclaimed.
        for key in ["PACKAGE_DIR", "CMD", "PATH", "DOCKER_CONFIG", "LD_PRELOAD", "bad-name", "1ABC"]:
            with self.subTest(key=key):
                pipeline = CONSUMER.replace('{"RAIN_API_KEY": "((rain.key))"}', '{"%s": "x"}' % key)
                result = self.render(pipeline)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("additional_params key", result.stderr)
                pipeline = CONSUMER.replace('test_on_zrh1_docker_host("compose-tests")',
                                            'test_on_zrh1_docker_host("compose-tests", {"%s": "x"})' % key)
                self.assertNotEqual(self.render(pipeline).returncode, 0)

    def test_invalid_workload_timeout_fails_render(self):
        for value in ["0", "1000", '"12"']:
            with self.subTest(value=value):
                pipeline = CONSUMER.replace("workload_timeout_minutes=40", "workload_timeout_minutes=" + value)
                self.assertNotEqual(self.render(pipeline).returncode, 0)


class Vendoring(unittest.TestCase):
    def test_referenced_tasks_exist_and_are_vendored_to_every_consumer(self):
        # vendir.tmpl.yml excludes feature-prefixed files for repos without that
        # feature; the transport must reach every repository.
        excluded = re.findall(r"- shared/ci/\*\*/([a-z]+)-\*", (ROOT / "vendir.tmpl.yml").read_text())
        self.assertIn("docker", excluded)
        text = FRAGMENTS.read_text()
        for name in ["ssh-host-run.sh", "ssh-host-check-cleanup.sh"]:
            self.assertIn("ci/vendor/tasks/" + name, text)
        tasks = sorted((ROOT / "shared/ci/tasks").glob("ssh-host-*.sh"))
        self.assertEqual([t.name for t in tasks], ["ssh-host-check-cleanup.sh", "ssh-host-remote-cleanup.sh",
                         "ssh-host-remote-preflight.sh", "ssh-host-remote-run.sh", "ssh-host-run.sh"])
        for task in tasks:
            self.assertFalse(any(task.name.startswith(prefix + "-") for prefix in excluded), task.name)
            self.assertTrue(os.access(task, os.X_OK), task.name)


if __name__ == "__main__":
    unittest.main()
