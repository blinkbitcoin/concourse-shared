"""Run ci/tasks/bump-shared-files.sh against stubbed vendir output."""
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci/tasks/bump-shared-files.sh"
DEPENDABOT = "version: 2\nupdates: []\n"

VENDIR_STUB = textwrap.dedent(f"""\
    #!/bin/bash
    # Stands in for `vendir sync`, run from source-repo/ci.
    set -eu
    [[ "$1" == "sync" ]]
    mkdir -p vendor/config vendor/tasks ../.github/workflows/vendor
    printf '{DEPENDABOT}' > vendor/config/nodejs-dependabot.yml
    printf 'shared: true\\n' > vendor/pipeline-fragments.lib.yml
    printf 'name: audit\\n' > ../.github/workflows/vendor/nodejs-audit.yml
    """)


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


class BumpSharedFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = dict(
            os.environ,
            HOME=str(self.tmp / "home"),
            XDG_CONFIG_HOME=str(self.tmp / "home"),
            GIT_CONFIG_NOSYSTEM="1",
            PATH=f"{self.tmp / 'bin'}:{os.environ['PATH']}",
            FEATURES="[]",
        )
        (self.tmp / "home").mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("vendir", VENDIR_STUB), ("rename", "#!/bin/sh\nexit 0\n")):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)

        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / "vendir.tmpl.yml").write_text((ROOT / "vendir.tmpl.yml").read_text())
        source = self.tmp / "source-repo"
        (source / ".github/workflows").mkdir(parents=True)
        (source / "README.md").write_text("consumer\n")
        for path in (repo, source):
            subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=self.env)
            subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=self.env)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "init"],
                cwd=path, check=True, env=self.env,
            )
        self.ref = git(repo, "rev-parse", "HEAD")
        self.source = source

    def run_script(self):
        subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.tmp, env=self.env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def test_vendor_tree_keeps_dependabot_config(self):
        self.run_script()
        vendored = self.source / "ci/vendor/config/nodejs-dependabot.yml"
        self.assertTrue(vendored.is_file(), "ci/vendor must remain an exact vendir snapshot")
        self.assertEqual(vendored.read_text(), DEPENDABOT)
        self.assertEqual((self.source / ".github/dependabot.yml").read_text(), DEPENDABOT)

    def test_generated_vendir_config_pins_ref_without_trailing_blank_line(self):
        self.run_script()
        config = (self.source / "ci/vendir.yml").read_text()
        self.assertIn(f"ref: {self.ref}", config)
        self.assertNotIn("ref: main", config)
        self.assertTrue(config.endswith("\n"))
        self.assertFalse(config.endswith("\n\n"))

    def test_changes_are_committed(self):
        self.run_script()
        self.assertEqual(git(self.source, "status", "--porcelain"), "")
        self.assertEqual(git(self.source, "log", "-1", "--format=%s"), "ci(shared): bump vendored ci files")


if __name__ == "__main__":
    unittest.main()
