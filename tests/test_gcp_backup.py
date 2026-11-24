"""Run ci/tasks/gcp-backup.sh against stubbed gcloud, gh-token and gh."""
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci/tasks/gcp-backup.sh"

GCLOUD_STUB = textwrap.dedent("""\
    #!/bin/bash
    echo "$*" >> "$STUB_LOG/gcloud"
    """)

# Mints a new token per call, like the GitHub App installation token endpoint.
GH_TOKEN_STUB = textwrap.dedent("""\
    #!/bin/bash
    set -eu
    [[ "$1" == "generate" ]]
    n=$(( $(cat "$STUB_LOG/mints" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_LOG/mints"
    if [[ -n "${STUB_FAIL_MINT_AT-}" && "$n" -ge "$STUB_FAIL_MINT_AT" ]]; then
      if [[ -n "${STUB_FAILED_MINT_OUTPUT+set}" ]]; then
        printf '%s' "$STUB_FAILED_MINT_OUTPUT"
      else
        echo '{"message":"Bad credentials"}'
      fi
      exit 0
    fi
    printf '{"token":"tok-%s"}\\n' "$n"
    """)

# Rejects a token once it has been used for STUB_TOKEN_MAX_USES calls, which
# stands in for the one-hour installation token lifetime.
GH_STUB = textwrap.dedent("""\
    #!/bin/bash
    set -eu
    uses_file="$STUB_LOG/uses-$GH_TOKEN"
    uses=$(( $(cat "$uses_file" 2>/dev/null || echo 0) + 1 ))
    echo "$uses" > "$uses_file"
    if [[ "$uses" -gt "${STUB_TOKEN_MAX_USES:-2}" ]]; then
      echo "HTTP 401: Bad credentials (https://api.github.com/graphql)" >&2
      exit 1
    fi
    case "$1 $2" in
      "repo list")
        printf '%s' "${STUB_REPOS-}" | tr ' ' '\\n' | grep . || true
        ;;
      "repo clone")
        echo "$3 $GH_TOKEN" >> "$STUB_LOG/clones"
        mkdir -p "$4"
        echo "$3" > "$4/README"
        ;;
      *) exit 2 ;;
    esac
    """)


class GcpBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "log"
        self.log.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("gcloud", GCLOUD_STUB), ("gh-token", GH_TOKEN_STUB), ("gh", GH_STUB)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            STUB_LOG=str(self.log),
            GOOGLE_CREDENTIALS="{}",
            GOOGLE_BUCKET_NAME="bucket",
            GH_APP_ID="1",
            GH_APP_PRIVATE_KEY="key",
            STUB_REPOS="blinkbitcoin/a blinkbitcoin/b blinkbitcoin/c blinkbitcoin/d",
        )
        self.env.pop("GH_TOKEN", None)

    def run_script(self):
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.work, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def uploads(self):
        log = self.log / "gcloud"
        return [l for l in log.read_text().splitlines() if l.startswith("storage cp")] if log.exists() else []

    def test_each_clone_uses_a_fresh_token(self):
        # A single token is only good for 2 calls here, so reusing the first
        # token for every clone (the old behaviour) would hit a 401.
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        clones = (self.log / "clones").read_text().split("\n")[:-1]
        self.assertEqual([c.split()[0] for c in clones],
                         ["blinkbitcoin/a", "blinkbitcoin/b", "blinkbitcoin/c", "blinkbitcoin/d"])
        tokens = [c.split()[1] for c in clones]
        self.assertEqual(len(set(tokens)), len(tokens), "each clone must get its own token")
        self.assertIn("cloning 4 repos from blinkbitcoin", result.stdout)
        self.assertEqual(len(self.uploads()), 1)
        tarballs = list(self.work.glob("blinkbitcoin-*.tar.gz"))
        self.assertEqual(len(tarballs), 1)
        listing = subprocess.check_output(["tar", "-tzf", str(tarballs[0])], text=True)
        self.assertIn("blinkbitcoin/d/README", listing)

    def test_failed_token_mint_stops_before_upload(self):
        self.env["STUB_FAIL_MINT_AT"] = "3"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("failed to generate a GitHub App installation token", result.stdout)
        self.assertEqual(self.uploads(), [])

    def test_empty_token_mint_stops_before_upload(self):
        self.env["STUB_FAIL_MINT_AT"] = "1"
        self.env["STUB_FAILED_MINT_OUTPUT"] = ""
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("failed to generate a GitHub App installation token", result.stdout)
        self.assertFalse((self.log / "clones").exists())
        self.assertEqual(self.uploads(), [])

    def test_github_401_stops_before_upload(self):
        self.env["STUB_TOKEN_MAX_USES"] = "0"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("HTTP 401", result.stdout)
        self.assertEqual(self.uploads(), [])

    def test_empty_repo_list_is_not_uploaded(self):
        self.env["STUB_REPOS"] = ""
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("refusing to upload an empty backup", result.stdout)
        self.assertEqual(self.uploads(), [])


if __name__ == "__main__":
    unittest.main()
