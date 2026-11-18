"""SSH Docker-host transport (shared/ci/tasks/ssh-host-*).

Network clients are stubbed; Bash, jq, timeout, traps, files and (on Linux)
the remote process supervisor run for real. No fixture can contact a guest,
cloud account or registry.
"""
import base64
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import shutil
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "shared/ci/tasks"
CLEANUP = (TASKS / "ssh-host-remote-cleanup.sh").read_text()
HAS_PROC = Path("/proc").exists()

STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
assert not any(k in os.environ for k in ("SSH_PRIVATE_KEY", "SSH_PUB_KEY", "GOOGLE_CREDENTIALS", "private_key", "google_credentials"))
body = sys.stdin.read() if name == "ssh" else ""
if name == "curl":
    assertion = next(arg for arg in args if arg.startswith("assertion@")).split("@", 1)[1]
    assertion_path = pathlib.Path(assertion)
    assert assertion_path.stat().st_mode & 0o777 == 0o600
    body = assertion_path.read_text()
log = pathlib.Path(os.environ["CALLS"])
calls = log.read_text().splitlines() if log.exists() else []
index = len(calls)
if name == "ssh":
    key = pathlib.Path(args[args.index("-i") + 1])
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.parent.stat().st_mode & 0o777 == 0o700
    for private in ("google.json", "sa.pem", "known_hosts", "env"):
        assert (key.parent / private).stat().st_mode & 0o777 == 0o600
    assert "StrictHostKeyChecking=yes" in args
known = next((a.split("=", 1)[1] for a in args if a.startswith("UserKnownHostsFile=")), None)
known_hosts = pathlib.Path(known).read_text() if known else None
with log.open("a") as f:
    f.write(json.dumps({"name": name, "args": args, "input": body, "known_hosts": known_hosts,
                        "known_hosts_path": known}) + "\n")
if str(index) in os.environ.get("FAIL", "").split(","):
    sys.exit(7)
if os.environ.get("BLOCK") == "workload" and name == "ssh" and "tail -n +1 -f --pid=" in body:
    pathlib.Path(os.environ["BLOCKED"]).write_text(str(os.getpid()))
    while True: time.sleep(1)
if str(index) == os.environ.get("BLOCK"):
    pathlib.Path(os.environ["BLOCKED"]).write_text(str(os.getpid()))
    while True: time.sleep(1)
if name == "ssh" and "mktemp" in args[-1]:
    print(os.environ.get("DIRECTORY", ".ci-run/run.12345678"))
if name == "gcloud":
    sys.exit(9)
if name == "curl":
    out = pathlib.Path(args[args.index("--output") + 1])
    out.write_text(json.dumps({"access_token": os.environ.get("TOKEN", "TOKEN-FIXTURE")}))
'''

SECRETS = ["PRIVATE-FIXTURE", "GOOGLE-FIXTURE", "TOKEN-FIXTURE", "PARAM-SECRET-FIXTURE"]


def script(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


class Transport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssh fixture ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        (self.root / "docker-host").mkdir()
        (self.root / "tmp").mkdir()
        self.meta = {"transport": "ssh", "docker_host_name": "docker-host-01",
                     "ssh_host": "10.100.1.2", "ssh_host_key": "ssh-ed25519 AAAATEST"}
        self.metadata_file = self.root / "docker-host/metadata"
        self.metadata_file.write_text(json.dumps(self.meta))
        binary = self.root / "bin"
        binary.mkdir()
        for name in ["ssh", "rsync", "gcloud", "curl"]:
            path = binary / name
            path.write_text(STUB)
            path.chmod(0o755)
        # Records the call and whether any secret file already existed on disk.
        script(binary / "make", 'printf "%s %s\\n" "$PWD" "$*" >> "$MAKE_CALLS"\n'
                                'ls -A "$TMPDIR" >> "$MAKE_CALLS"\n'
                                'if [[ ${MAKE_PLANT:-} == file ]]; then cp ../docker-host/metadata ../host-cleanup/metadata;'
                                ' elif [[ ${MAKE_PLANT:-} == dir ]]; then mkdir -p ../host-cleanup/metadata/x; fi\n'
                                'exit "${MAKE_STATUS:-0}"')
        self.log = self.root / "calls"
        key = self.root / "fixture.pem"
        subprocess.check_call(["openssl", "genrsa", "-out", str(key), "2048"], stderr=subprocess.DEVNULL)
        pkcs8 = self.root / "fixture.pkcs8.pem"
        subprocess.check_call(["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", str(key), "-out", str(pkcs8)],
                              stderr=subprocess.DEVNULL)
        self.pem = pkcs8.read_text()
        credentials = json.dumps({
            "type": "service_account",
            "client_email": "ci-fixture@example.invalid",
            "private_key": self.pem,
            "private_key_id": "GOOGLE-FIXTURE",
            "token_uri": "https://evil.example/token",
        })
        self.env = dict(os.environ, PATH=str(binary) + ":" + os.environ["PATH"],
            TMPDIR=str(self.root / "tmp"), CALLS=str(self.log), BLOCKED=str(self.root / "blocked"),
            MAKE_CALLS=str(self.root / "make-calls"),
            REPO_PATH="repo", SSH_HOST_MODE="nix", WORKLOAD_TIMEOUT_MINUTES="45", CMD="echo ok",
            SSH_PRIVATE_KEY="PRIVATE-FIXTURE", SSH_PUB_KEY="PUBLIC-FIXTURE", GOOGLE_CREDENTIALS=credentials)
        for key in ["PACKAGE_DIR", "FAIL", "TOKEN", "DIRECTORY", "BLOCK", "PARAMS_KEYS", "TEST_CONTAINER"]:
            self.env.pop(key, None)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def invoke(self, **updates):
        self.log.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(TASKS / "ssh-host-run.sh")], cwd=self.root,
            env=dict(self.env, **updates), capture_output=True, text=True, timeout=20)
        self.assertFalse(list((self.root / "tmp").iterdir()), "local credentials leaked")
        for secret in SECRETS:
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertNotIn(secret, json.dumps([c["args"] for c in self.calls()]))
        return result

    def released(self):
        receipt = self.root / "host-cleanup/metadata"
        return receipt.exists() and receipt.read_text() == self.metadata_file.read_text()

    def assert_host_key_pinned(self, calls):
        ssh_calls = [c for c in calls if c["name"] == "ssh"]
        self.assertTrue(ssh_calls)
        for call in ssh_calls:
            self.assertEqual(call["known_hosts"], f"{self.meta['ssh_host']} {self.meta['ssh_host_key']}\n")
            key = call["args"][call["args"].index("-i") + 1]
            self.assertEqual(call["known_hosts_path"], str(Path(key).parent / "known_hosts"))
            self.assertEqual(call["args"][-2], f"ci@{self.meta['ssh_host']}")

    def remote_args(self, call):
        # Decode the actual SSH command with Bash, preserving multiline commands.
        decoded = subprocess.check_output(["bash", "-c", "set -- " + call["args"][-1] + '; printf "%s\\0" "$@"'])
        return decoded.decode().split("\0")[:-1]

    def test_success_pins_key_scopes_registry_and_removes_local_secrets(self):
        command = "printf '%s\n' \"quoted $value\"; echo $(literal)\nexit 0"
        result = self.invoke(CMD=command, PACKAGE_DIR="core/api")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([c["name"] for c in calls],
                         ["ssh", "ssh", "ssh", "rsync", "curl", "ssh", "ssh", "ssh", "ssh"])
        self.assertEqual(calls[2]["args"][-1], "docker ps -aq | xargs -r docker rm -fv && sudo -n rm -rf -- repo")
        login = next(c for c in calls if "login us.gcr.io" in c["args"][-1])
        self.assertEqual(login["input"], "TOKEN-FIXTURE\n")
        self.assertIn(".ci-run/run.12345678/docker", login["args"][-1])
        minted = next(c for c in calls if c["name"] == "curl")
        self.assertEqual(minted["args"][-1], "https://oauth2.googleapis.com/token")
        self.assertNotIn("evil.example", json.dumps(minted["args"]))
        self.assertNotIn("cloud-platform", minted["input"])
        self.assertFalse(any(c["name"] == "gcloud" for c in calls))
        self.assertNotIn("BEGIN PRIVATE KEY", json.dumps(calls))
        header_b64, payload_b64, sig_b64 = minted["input"].split(".")

        def dec(part):
            return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        self.assertEqual(json.loads(dec(header_b64))["alg"], "RS256")
        payload = json.loads(dec(payload_b64))
        self.assertEqual(payload["scope"], "https://www.googleapis.com/auth/devstorage.read_only")
        self.assertEqual(payload["aud"], "https://oauth2.googleapis.com/token")
        self.assertEqual(payload["iss"], "ci-fixture@example.invalid")
        # Google rejects assertions that live longer than one hour.
        self.assertEqual(payload["exp"] - payload["iat"], 3600)
        self.assertTrue(time.time() - 120 <= payload["iat"] <= time.time())
        self.assert_host_key_pinned(calls)
        signed = self.root / "signed"
        signature = self.root / "sig.bin"
        public = self.root / "pub.pem"
        signed.write_bytes(f"{header_b64}.{payload_b64}".encode())
        signature.write_bytes(dec(sig_b64))
        subprocess.check_call(["openssl", "rsa", "-in", str(self.root / "fixture.pkcs8.pem"), "-pubout",
                               "-out", str(public)], stderr=subprocess.DEVNULL)
        subprocess.check_call(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature",
                               str(signature), str(signed)], stdout=subprocess.DEVNULL)
        self.assertEqual(calls[-1]["input"], CLEANUP)
        self.assertEqual(calls[-2]["input"], (TASKS / "ssh-host-remote-run.sh").read_text())
        self.assertEqual(self.remote_args(calls[-2]),
                         ["bash", "-s", "--", ".ci-run/run.12345678", "repo", "core/api", "nix", "45", command])
        self.assertEqual(calls[-3]["args"][-1], "umask 077; cat > .ci-run/run.12345678/env")
        self.assertEqual(calls[-3]["input"], "")
        rsync = next(c for c in calls if c["name"] == "rsync")
        ssh_args = shlex.split(rsync["args"][rsync["args"].index("-e") + 1])
        self.assertEqual(ssh_args, ["ssh", *calls[0]["args"][:-2]])
        self.assertEqual((self.root / "host-cleanup/metadata").read_text(), self.metadata_file.read_text())

    def test_params_reach_host_only_through_private_env_file(self):
        value = "PARAM-SECRET-FIXTURE with 'quotes' \"double\" $dollar `tick`\nnext line"
        result = self.invoke(PARAMS_KEYS="RAIN_API_KEY EMPTY_PARAM UNSET_PARAM", RAIN_API_KEY=value, EMPTY_PARAM="")
        self.assertEqual(result.returncode, 0, result.stderr)
        upload = next(c for c in self.calls() if c["args"][-1].endswith("cat > .ci-run/run.12345678/env"))
        decoded = subprocess.check_output(["bash", "-c", upload["input"] + '\nprintf "%s" "$RAIN_API_KEY"'],
                                          env={"PATH": os.environ["PATH"]})
        self.assertEqual(decoded.decode(), value)
        self.assertNotIn("EMPTY_PARAM", upload["input"])
        self.assertNotIn("UNSET_PARAM", upload["input"])
        self.assert_host_key_pinned(self.calls())

    def test_invalid_or_reserved_params_refuse_before_remote_access(self):
        (self.root / "repo/a-file").write_text("glob target")
        for keys in ["bad-name", "1ABC", "PATH", "CMD", "DOCKER_CONFIG", "LD_PRELOAD", "CI_RUN_DIR", "OK *"]:
            with self.subTest(keys=keys):
                self.assertNotEqual(self.invoke(PARAMS_KEYS=keys).returncode, 0)
                self.assertEqual(self.calls(), [])
                # The host was never contacted, so the claim is released.
                self.assertTrue(self.released())

    def test_env_ci_is_substituted_on_worker_without_transport_credentials(self):
        (self.root / "repo/.env.ci").write_text("A=${RAIN_API_KEY}\nB=${SSH_PRIVATE_KEY}\nC=${GOOGLE_CREDENTIALS}\n")
        result = self.invoke(PARAMS_KEYS="RAIN_API_KEY", RAIN_API_KEY="PARAM-SECRET-FIXTURE")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "repo/tmp.env.ci").read_text(), "A=PARAM-SECRET-FIXTURE\nB=\nC=\n")

    def test_compose_mode_prepares_env_and_passes_validated_container(self):
        # A failing optional make target is ignored, as on the GCP hosts.
        for make_status in ["0", "2"]:
            with self.subTest(make_status=make_status):
                (self.root / "make-calls").unlink(missing_ok=True)
                result = self.invoke(SSH_HOST_MODE="compose", TEST_CONTAINER="integration-tests", CMD="",
                                     MAKE_STATUS=make_status)
                self.assertEqual(result.returncode, 0, result.stderr)
                # Repository code runs before any key file exists in TMPDIR.
                self.assertEqual((self.root / "make-calls").read_text(),
                                 f"{(self.root / 'repo').resolve()} create-tmp-env-ci\n")
        workload = self.calls()[-2]
        self.assertEqual(self.remote_args(workload),
                         ["bash", "-s", "--", ".ci-run/run.12345678", "repo", ".", "compose", "45", "integration-tests"])
        for container in ["", "-x", "a b", "a;b", "a/b"]:
            with self.subTest(container=container):
                self.assertNotEqual(self.invoke(SSH_HOST_MODE="compose", TEST_CONTAINER=container).returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertTrue(self.released())

    def test_receipt_planted_by_repository_code_cannot_release(self):
        # make runs repository code in this task; only cleanup may write the receipt.
        self.assertEqual(self.invoke(SSH_HOST_MODE="compose", TEST_CONTAINER="integration-tests").returncode, 0)
        cleanup = len(self.calls()) - 1
        result = self.invoke(SSH_HOST_MODE="compose", TEST_CONTAINER="integration-tests", MAKE_PLANT="file",
                             FAIL=str(cleanup))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup failed", result.stderr)
        self.assertFalse((self.root / "host-cleanup/metadata").exists())
        # A planted directory must not stop the remote cleanup; the confirmed
        # cleanup then replaces it with the real receipt.
        result = self.invoke(SSH_HOST_MODE="compose", TEST_CONTAINER="integration-tests", MAKE_PLANT="dir")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["input"], CLEANUP)
        self.assertTrue(self.released())

    def test_missing_client_dependencies_refuse_before_remote_access(self):
        # Do not let tools installed on the test runner hide an incomplete image.
        binary = self.root / "bin"
        available = ["bash", "python3", "jq", "dirname", "mktemp", "rm", "mkdir", "openssl", "timeout", "envsubst"]
        for name in available:
            (binary / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(binary)
        for missing in ["envsubst", "timeout", "rsync", "ssh", "curl", "openssl", "jq"]:
            with self.subTest(missing=missing):
                (binary / missing).unlink()
                result = self.invoke()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"task image is missing {missing}", result.stderr)
                self.assertEqual(self.calls(), [])
                # Checked before the claim is validated: kept conservatively.
                self.assertFalse(self.released())

    def test_every_external_failure_cleans_up_and_preserves_status(self):
        self.assertEqual(self.invoke().returncode, 0)
        count = len(self.calls())
        for index in range(count):
            with self.subTest(index=index):
                result = self.invoke(FAIL=str(index))
                self.assertEqual(result.returncode, 1 if index == count - 1 else 7, result.stderr)
                if 2 <= index < count - 1:
                    self.assertEqual(self.calls()[-1]["input"], CLEANUP)

    def test_cleanup_receipt_and_release_are_independent_of_workload_status(self):
        self.assertEqual(self.invoke().returncode, 0)
        count = len(self.calls())
        for failures, releasable in [("", True), (str(count - 2), True),
                                     (str(count - 1), False), ("0", False), ("1", False),
                                     (f"{count - 2},{count - 1}", False)]:
            with self.subTest(failures=failures):
                # invoke reuses the directory, exercising stale receipt removal.
                self.invoke(FAIL=failures)
                gate = subprocess.run(["bash", str(TASKS / "ssh-host-check-cleanup.sh")],
                                      cwd=self.root, capture_output=True, text=True)
                self.assertEqual(gate.returncode == 0, releasable, gate.stderr)

    def test_receipt_write_failure_retains_claim_and_removes_local_secrets(self):
        script(self.root / "bin/cp", "exit 1")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "host-cleanup/metadata").exists())

    def test_cleanup_failure_preserves_workload_failure(self):
        self.assertEqual(self.invoke().returncode, 0)
        count = len(self.calls())
        result = self.invoke(FAIL=f"{count - 2},{count - 1}")
        self.assertEqual(result.returncode, 7)
        self.assertIn("cleanup failed", result.stderr)

    def test_timeout_cleans_up(self):
        # Shorten only the workload deadline (45m + 200s); exercise real GNU timeout.
        script(self.root / "bin/timeout",
               'if [[ $2 == 2900s ]]; then set -- "$1" 0.2s "${@:3}"; fi\n'
               "exec " + shlex.quote(shutil.which("timeout")) + ' "$@"')
        result = self.invoke(BLOCK="workload")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(self.calls()[-1]["input"], CLEANUP)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "blocked").read_text()), 0)

    def test_unsupported_temporary_path_cleans_up(self):
        for suffix in ['quote"', 'back\\slash']:
            directory = self.root / "tmp" / suffix
            directory.mkdir()
            self.log.unlink(missing_ok=True)
            result = subprocess.run(["bash", str(TASKS / "ssh-host-run.sh")], cwd=self.root,
                env=dict(self.env, TMPDIR=str(directory)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(list(directory.iterdir()))
            # Refused before any SSH command, so nothing to clean up remotely.
            self.assertEqual(self.calls(), [])
            self.assertTrue(self.released())
            directory.rmdir()

    def test_missing_token_and_bad_directory_fail_closed(self):
        self.assertNotEqual(self.invoke(TOKEN="").returncode, 0)
        self.assertEqual(self.calls()[-1]["input"], CLEANUP)
        for directory in ["/wrong", ".ci-run/run.12345678; touch escaped", "../run.12345678"]:
            self.assertNotEqual(self.invoke(DIRECTORY=directory).returncode, 0)
            self.assertEqual(len(self.calls()), 2)

    def test_reject_untrusted_metadata_paths_modes_and_timeouts(self):
        for changes in [{"transport": "gcp"}, {"docker_host_name": "invalid name"}, {"docker_host_name": "host\n"},
                        {"docker_host_name": "a" * 64}, {"ssh_host": "8.8.8.8"}, {"ssh_host": "127.0.0.1"},
                        {"ssh_host": "bad"}, {"ssh_host": "10.01.1.1"}, {"ssh_host": "10.0.0.256"},
                        {"ssh_host": "10.0.0.1\n"}, {"ssh_host": "172.32.0.1"}, {"ssh_host": "172.15.0.1"},
                        {"ssh_host": None}, {"ssh_host_key": "ssh-ed25519 key\nextra"},
                        {"ssh_host_key": "ssh-ed25519 key\n"}]:
            with self.subTest(changes=changes):
                self.metadata_file.write_text(json.dumps(dict(self.meta, **changes)))
                self.assertNotEqual(self.invoke().returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.root / "host-cleanup/metadata").exists())
        for value in ["not JSON", "null", "[]", json.dumps(self.meta) + "\n" + json.dumps(self.meta)]:
            self.metadata_file.write_text(value)
            self.assertNotEqual(self.invoke().returncode, 0)
            self.assertEqual(self.calls(), [])
            self.assertFalse((self.root / "host-cleanup/metadata").exists())
        self.metadata_file.write_text(json.dumps(self.meta))
        # REPO_PATH is removed with sudo on the host: one plain, non-dot component.
        for path in ["/root", "../repo", "repo/../other", "-option", "a b", "", ".", "..", ".ci-run", "repo/sub"]:
            with self.subTest(REPO_PATH=path):
                self.assertNotEqual(self.invoke(REPO_PATH=path).returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertTrue(self.released())
        for path in ["/root", "../repo", "repo/../other", "-option", "a b", ""]:
            with self.subTest(PACKAGE_DIR=path):
                self.assertNotEqual(self.invoke(PACKAGE_DIR=path).returncode, 0)
                self.assertEqual(self.calls(), [])
        for changes in [{"SSH_HOST_MODE": ""}, {"SSH_HOST_MODE": "gcp"}, {"WORKLOAD_TIMEOUT_MINUTES": ""},
                        {"WORKLOAD_TIMEOUT_MINUTES": "0"}, {"WORKLOAD_TIMEOUT_MINUTES": "1000"},
                        {"WORKLOAD_TIMEOUT_MINUTES": "5m"}, {"WORKLOAD_TIMEOUT_MINUTES": "07"}]:
            with self.subTest(changes=changes):
                self.assertNotEqual(self.invoke(**changes).returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertTrue(self.released())
        for address in ["10.1.2.3", "172.16.1.2", "172.31.255.255", "192.168.1.2"]:
            self.metadata_file.write_text(json.dumps(dict(self.meta, ssh_host=address)))
            self.assertEqual(self.invoke().returncode, 0)

    def test_invalid_service_account_does_not_open_ssh(self):
        for creds in ["not-json",
                      json.dumps({"type": "service_account", "client_email": "not-an-email", "private_key": self.pem}),
                      json.dumps({"type": "service_account", "client_email": "ci@example.invalid",
                                  "private_key": "not-a-key", "token_uri": "https://evil.example/token"})]:
            with self.subTest(creds=creds[:24]):
                self.assertNotEqual(self.invoke(GOOGLE_CREDENTIALS=creds).returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertTrue(self.released())

    def test_missing_inputs_refuse_before_remote_access(self):
        for update in [{"CMD": " "}, {"SSH_PRIVATE_KEY": ""}, {"GOOGLE_CREDENTIALS": ""}, {"REPO_PATH": "missing"}]:
            self.assertNotEqual(self.invoke(**update).returncode, 0)
            self.assertEqual(self.calls(), [])
            self.assertTrue(self.released())

    def test_cancellation_runs_cleanup_and_removes_secrets(self):
        for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
            with self.subTest(signal=sig):
                self.log.unlink(missing_ok=True)
                blocked = self.root / "blocked"
                blocked.unlink(missing_ok=True)
                process = subprocess.Popen(["bash", str(TASKS / "ssh-host-run.sh")], cwd=self.root,
                    env=dict(self.env, BLOCK="workload"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while not blocked.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(blocked.exists())
                    process.send_signal(sig)
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + sig, stderr)
                    self.assertEqual(self.calls()[-1]["input"], CLEANUP)
                    self.assertFalse(list((self.root / "tmp").iterdir()))
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(blocked.read_text()), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()


class RemoteCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / ".ci-run/run.12345678"
        self.run_dir.mkdir(parents=True)

    def cleanup(self, env=None, run_dir=".ci-run/run.12345678"):
        return subprocess.run(["bash", str(TASKS / "ssh-host-remote-cleanup.sh"), run_dir], cwd=self.root,
            env=env or dict(os.environ, HOME=str(self.root)), capture_output=True, text=True, timeout=10)

    def credentials(self):
        (self.run_dir / "docker").mkdir(exist_ok=True)
        (self.run_dir / "docker/config.json").write_text("fixture-token")
        (self.run_dir / "env").write_text("export PARAM=fixture\n")

    def assert_credentials_removed(self):
        self.assertFalse((self.run_dir / "docker").exists())
        self.assertFalse((self.run_dir / "env").exists())

    def test_without_live_pid(self):
        for pid in [None, "999999999"]:
            self.credentials()
            if pid is not None:
                (self.run_dir / "runner.pid").write_text(pid)
            result = self.cleanup()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_credentials_removed()

    def test_process_listing_failure_does_not_confirm_cleanup(self):
        self.credentials()
        (self.run_dir / "runner.pid").write_text("999999999")
        binary = self.root / "bin"
        binary.mkdir()
        script(binary / "ps", "exit 1")
        result = self.cleanup(dict(os.environ, HOME=str(self.root), PATH=str(binary) + ":" + os.environ["PATH"]))
        self.assertNotEqual(result.returncode, 0)
        self.assert_credentials_removed()

    def test_invalid_pid_and_run_directory(self):
        self.credentials()
        (self.run_dir / "runner.pid").write_text("not-a-pid")
        self.assertNotEqual(self.cleanup().returncode, 0)
        self.assert_credentials_removed()
        for run_dir in ["/tmp", "../run.12345678", ".ci-run/run.1234", ".ci-run/run.12345678/.."]:
            self.assertNotEqual(self.cleanup(run_dir=run_dir).returncode, 0)

    def test_prunes_old_runs_and_keeps_active_log(self):
        base = self.root / ".ci-run"
        shutil.rmtree(base)
        base.mkdir()
        names = []
        for index in range(6):
            path = base / f"run.{index + 1:08d}"
            path.mkdir()
            (path / "log").write_text(f"log-{index}")
            (path / "docker").mkdir()
            (path / "docker/config.json").write_text("token")
            os.utime(path, (index + 1, index + 1))
            names.append(path.name)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("keep")
        link = base / "run.symlink0"
        link.symlink_to(outside, target_is_directory=True)
        (base / "run.notadir").write_text("file")
        active = f".ci-run/{names[-1]}"
        result = self.cleanup(run_dir=active)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / active / "log").read_text(), "log-5")
        self.assertFalse((self.root / active / "docker").exists())
        remaining = sorted(path.name for path in base.iterdir() if path.is_dir() and not path.is_symlink())
        self.assertEqual(remaining, names[-4:])
        self.assertEqual((outside / "secret").read_text(), "keep")
        self.assertTrue(link.is_symlink())
        self.assertEqual((base / "run.notadir").read_text(), "file")

    def test_rejects_unpublished_pid_after_launch_attempt(self):
        # command.sh exists before setsid can start the workload. Even an
        # empty file means startup may have been interrupted before PID write.
        command = self.run_dir / "command.sh"
        command.write_text('trap "" TERM; touch "$CI_RUN_DIR/ready"; while :; do sleep 1; done\n')
        process = subprocess.Popen(["bash", str(command)], start_new_session=True,
            env=dict(os.environ, CI_RUN_DIR=str(self.run_dir)))
        try:
            deadline = time.monotonic() + 3
            while not (self.run_dir / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.run_dir / "ready").exists())
            for pid, marker in [(None, command.read_text()), (None, ""), ("", ""), ("not-a-pid", "")]:
                with self.subTest(pid=pid, empty_marker=not marker):
                    command.write_text(marker)
                    (self.run_dir / "runner.pid").unlink(missing_ok=True)
                    self.credentials()
                    if pid is not None:
                        (self.run_dir / "runner.pid").write_text(pid)
                    result = self.cleanup()
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("termination could not be confirmed", result.stderr)
                    self.assertIsNone(process.poll(), "untracked workload must not be guessed at")
                    self.assert_credentials_removed()
                    self.assertTrue(command.exists(), "retain launch evidence")
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)

    @unittest.skipUnless(HAS_PROC, "Remote cleanup fixture requires Linux")
    def test_requires_matching_process_marker(self):
        for matching in [True, False]:
            with self.subTest(matching=matching):
                self.credentials()
                process = subprocess.Popen(["setsid", "sleep", "300"],
                    env=dict(os.environ, CI_RUN_DIR=str(self.run_dir) if matching else "unrelated"))
                try:
                    deadline = time.monotonic() + 3
                    while os.getpgid(process.pid) != process.pid and time.monotonic() < deadline:
                        time.sleep(0.01)
                    (self.run_dir / "runner.pid").write_text(str(process.pid))
                    result = self.cleanup()
                    self.assertEqual(result.returncode, 0 if matching else 1, result.stderr)
                    self.assert_credentials_removed()
                    if matching:
                        self.assertEqual(process.wait(timeout=3), -15)
                    else:
                        self.assertIsNone(process.poll())
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)

    @unittest.skipUnless(HAS_PROC, "Process-group cleanup requires Linux")
    def test_escalates_term_resistant_group_and_fails_if_kill_fails(self):
        for kill_fails in [False, True]:
            with self.subTest(kill_fails=kill_fails):
                ready = self.run_dir / "ready"
                ready.unlink(missing_ok=True)
                process = subprocess.Popen(["bash", "-c",
                    'trap "" TERM; touch "$CI_RUN_DIR/ready"; while :; do sleep 1; done'],
                    start_new_session=True, env=dict(os.environ, CI_RUN_DIR=str(self.run_dir)))
                try:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    (self.run_dir / "runner.pid").write_text(str(process.pid))
                    self.credentials()
                    env = dict(os.environ, HOME=str(self.root))
                    if kill_fails:
                        stub = self.root / "kill-fails.sh"
                        stub.write_text('kill() { return 1; }\n')
                        env["BASH_ENV"] = str(stub)
                    result = self.cleanup(env)
                    self.assertEqual(result.returncode, 1 if kill_fails else 0, result.stderr)
                    self.assert_credentials_removed()
                    if kill_fails:
                        self.assertIsNone(process.poll())
                    else:
                        self.assertEqual(process.wait(timeout=3), -signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)


class ReleaseGate(unittest.TestCase):
    def test_only_matching_ssh_receipt_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-host").mkdir()
            metadata = root / "docker-host/metadata"
            receipt = root / "host-cleanup/metadata"

            def gate():
                return subprocess.run(["bash", str(TASKS / "ssh-host-check-cleanup.sh")],
                                      cwd=root, capture_output=True).returncode
            self.assertNotEqual(gate(), 0)
            metadata.write_text('{"transport":"ssh"}')
            self.assertNotEqual(gate(), 0)
            receipt.parent.mkdir()
            receipt.write_text('{"transport":"ssh","host":"different"}')
            self.assertNotEqual(gate(), 0)
            receipt.write_text(metadata.read_text())
            self.assertEqual(gate(), 0)
            # A matching receipt cannot release a non-SSH or malformed claim.
            for value in [json.dumps({"transport": "gcp"}), "{}", "invalid"]:
                with self.subTest(metadata=value):
                    metadata.write_text(value)
                    receipt.write_text(value)
                    self.assertNotEqual(gate(), 0)


class Preflight(unittest.TestCase):
    def test_identity_services_canonical_storage_and_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, body in {
                "hostname": 'echo "$HOSTNAME_FIXTURE"', "docker": 'exit "$DOCKER_STATUS"',
                "nix": 'exit "$NIX_STATUS"', "containerd": 'printf "%s\\n" "$CONFIG"; exit "$CONTAINERD_STATUS"',
                "findmnt": 'echo "$MOUNT"',
            }.items():
                script(root / name, body)
            # Keep the readiness check hermetic without requiring /var writes.
            env_file = root / "env.sh"
            env_file.write_text('test() { if [[ $* == "-f /var/lib/docker-host/ready" ]]; then [[ $READY == 1 ]];'
                                ' else builtin test "$@"; fi; }\n')
            base = dict(os.environ, PATH=str(root) + ":" + os.environ["PATH"], BASH_ENV=str(env_file),
                HOSTNAME_FIXTURE="docker-host-01", READY="1", DOCKER_STATUS="0", NIX_STATUS="0",
                CONTAINERD_STATUS="0",
                CONFIG='version = 2\nroot = "/var/lib/docker-host/containerd"\n[grpc]\naddress = "fixture"',
                MOUNT="/var/lib/docker-host")
            cases = [({}, True), ({"CONFIG": "root = '/var/lib/docker-host/containerd'\n[grpc]"}, True),
                     ({"HOSTNAME_FIXTURE": "wrong"}, False), ({"READY": "0"}, False),
                     ({"DOCKER_STATUS": "1"}, False), ({"NIX_STATUS": "1"}, False),
                     ({"CONTAINERD_STATUS": "1"}, False), ({"MOUNT": "/"}, False),
                     ({"CONFIG": 'root = "/var/lib/containerd"'}, False), ({"CONFIG": ''}, False),
                     ({"CONFIG": '[plugin]\nroot = "/var/lib/docker-host/containerd"'}, False),
                     ({"CONFIG": 'root = "/var/lib/docker-host/containerd"\nroot = "/var/lib/containerd"'}, False)]
            for changes, expected in cases:
                with self.subTest(changes=changes):
                    result = subprocess.run(["bash", str(TASKS / "ssh-host-remote-preflight.sh"), "docker-host-01"],
                        env=dict(base, **changes), capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, expected, result.stderr)


@unittest.skipUnless(HAS_PROC and shutil.which("setsid"), "Remote supervisor integration requires Linux")
class RemoteRunner(unittest.TestCase):
    """Exercise the actual remote supervisor with fake nix and docker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        self.run_dir = self.root / ".ci-run/run.12345678"
        self.run_dir.mkdir(parents=True)
        binary = self.root / "bin"
        binary.mkdir()
        script(binary / "nix", 'shift 2\nexec "$@"')
        script(binary / "docker", r'''
printf '%s|%s|%s|%s\n' "$*" "$DOCKER_CONFIG" "${COMPOSE_PROJECT_NAME:-}" "${PARAM_FIXTURE:-}" >> "$DOCKER_LOG"
case "$1 $2" in
  "compose pull"|"compose down") exit 0 ;;
  "compose -f") [[ -z ${UP_SLEEP:-} ]] || sleep "$UP_SLEEP"; exit "$UP_STATUS" ;;
esac
case $1 in
  ps) [[ -z $CONTAINER_IDS ]] || printf '%s\n' $CONTAINER_IDS; exit 0 ;;
  inspect) echo "$TEST_EXIT"; exit "${INSPECT_STATUS:-0}" ;;
esac
exit 64''')
        self.env = dict(os.environ, HOME=str(self.root), PATH=str(binary) + ":" + os.environ["PATH"],
                        DOCKER_LOG=str(self.root / "docker-log"))

    def run_remote(self, mode, payload, package=".", minutes="45", **env):
        return subprocess.run(["bash", str(TASKS / "ssh-host-remote-run.sh"), ".ci-run/run.12345678", "repo",
                               package, mode, minutes, payload],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=20)

    def test_nix_success_failure_params_and_background_children_are_bounded(self):
        value = "multi word\nline $not_expanded"
        (self.run_dir / "env").write_text(f"export PARAM_FIXTURE={shlex.quote(value)}\n")
        for code in [0, 7]:
            with self.subTest(code=code):
                command = f'printf "%s" "$PARAM_FIXTURE" > param; sleep 300 & echo $! > child.pid; echo fixture; exit {code}'
                result = self.run_remote("nix", command)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn("fixture", result.stdout)
                self.assertEqual((self.root / "repo/param").read_text(), value)
                pid = (self.root / "repo/child.pid").read_text().strip()
                proc = Path("/proc") / pid / "stat"
                self.assertTrue(not proc.exists() or proc.read_text().split()[2] == "Z")
                (self.run_dir / "status").unlink()

    def test_compose_reports_test_container_status_and_always_tears_down(self):
        (self.run_dir / "env").write_text("export PARAM_FIXTURE=from-env\n")
        for up, ids, test_exit, inspect, expected in [
                ("0", "abc123", "0", "0", 0), ("0", "abc123", "3", "0", 3), ("2", "abc123", "0", "0", 2),
                ("0", "", "0", "0", 1), ("0", "abc123 def456", "0", "0", 1),
                ("0", "abc123", "not-a-number", "0", 1), ("0", "abc123", "0", "1", 1)]:
            with self.subTest(up=up, ids=ids, test_exit=test_exit, inspect=inspect):
                (self.root / "docker-log").unlink(missing_ok=True)
                (self.run_dir / "status").unlink(missing_ok=True)
                result = self.run_remote("compose", "integration-tests", UP_STATUS=up, CONTAINER_IDS=ids,
                                         TEST_EXIT=test_exit, INSPECT_STATUS=inspect)
                self.assertEqual(result.returncode, expected, result.stderr)
                log = [line.split("|") for line in (self.root / "docker-log").read_text().splitlines()]
                commands = [entry[0] for entry in log]
                self.assertEqual(commands[0], "compose pull")
                self.assertEqual(commands[1], "compose -f docker-compose.yml up integration-tests --attach integration-tests")
                self.assertEqual(commands[2], "ps -aq -f status=exited -f label=com.docker.compose.project=repo"
                                              " -f label=com.docker.compose.service=integration-tests")
                self.assertEqual(commands[-1], "compose down --remove-orphans --timeout 1")
                for _, config, project, param in log:
                    self.assertEqual(config, str(self.run_dir / "docker"))
                    self.assertEqual(project, "repo")
                    self.assertEqual(param, "from-env")

    def test_compose_timeout_still_runs_compose_down(self):
        # timeout signals only compose-test.sh; the stack must still come down.
        script(self.root / "bin/timeout",
               'if [[ $1 == --foreground && $3 == 1m ]]; then set -- --foreground --kill-after=1s 1s "${@:4}"; fi\n'
               "exec " + shlex.quote(shutil.which("timeout")) + ' "$@"')
        (self.run_dir / "env").write_text("export PARAM_FIXTURE=from-env\n")
        result = self.run_remote("compose", "integration-tests", minutes="1", UP_STATUS="0", UP_SLEEP="30",
                                 CONTAINER_IDS="abc123", TEST_EXIT="0")
        self.assertEqual(result.returncode, 124, result.stderr)
        log = [line.split("|") for line in (self.root / "docker-log").read_text().splitlines()]
        commands = [entry[0] for entry in log]
        self.assertEqual(commands[:2], ["compose pull",
                                        "compose -f docker-compose.yml up integration-tests --attach integration-tests"])
        self.assertEqual(commands[-1], "compose down --remove-orphans --timeout 1")
        self.assertEqual(log[-1][2], "repo")

    def test_cancellation_kills_term_ignoring_children_and_missing_path_fails(self):
        for mode in ["cancel", "missing"]:
            with self.subTest(mode=mode):
                command = 'trap "" TERM; sleep 300 & echo $! > child.pid; while :; do sleep 1; done'
                proc = subprocess.Popen(["bash", str(TASKS / "ssh-host-remote-run.sh"), ".ci-run/run.12345678",
                                         "repo", "." if mode == "cancel" else "missing", "nix", "45", command],
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    if mode == "cancel":
                        # The redirection creates child.pid before echo fills it;
                        # an empty pid would make the check below read /proc/stat.
                        pid_file = self.root / "repo/child.pid"
                        deadline = time.monotonic() + 5
                        while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(pid_file.read_text().strip().isdigit())
                        proc.terminate()
                    proc.communicate(timeout=10)
                    self.assertEqual(proc.returncode, 143 if mode == "cancel" else 1)
                    if mode == "cancel":
                        pid = (self.root / "repo/child.pid").read_text().strip()
                        stat = Path("/proc") / pid / "stat"
                        # SIGKILL is delivered asynchronously: the runner can
                        # exit just before the child is seen as dead.
                        def gone():
                            try:
                                return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
                            except (FileNotFoundError, ProcessLookupError):
                                return True
                        deadline = time.monotonic() + 5
                        while not gone() and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(gone(), stat.read_text() if stat.exists() else "")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                (self.run_dir / "status").unlink(missing_ok=True)

    def test_status_is_read_only_when_complete(self):
        # An empty status file stands in for a write the supervisor observes
        # before its content lands; the real exit status must still win.
        result = self.run_remote("nix", ': > "$CI_RUN_DIR/status"; sleep 2; exit 7')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual((self.run_dir / "status").read_text(), "7\n")
        self.assertFalse((self.run_dir / "status.tmp").exists())

    def test_unreadable_params_file_fails_workload_without_running_it(self):
        (self.run_dir / "env").write_text("export BROKEN='unterminated\n")
        result = self.run_remote("nix", "touch ran")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.root / "repo/ran").exists())

    def test_rejects_unknown_mode_and_timeout_before_launch(self):
        for mode, minutes in [("gcp", "45"), ("nix", "0"), ("nix", "45m")]:
            with self.subTest(mode=mode, minutes=minutes):
                result = self.run_remote(mode, "true", minutes=minutes)
                self.assertEqual(result.returncode, 1)
                self.assertFalse((self.run_dir / "command.sh").exists())


if __name__ == "__main__":
    unittest.main()
