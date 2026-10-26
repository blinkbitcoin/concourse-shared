#!/usr/bin/env python3
"""Credential-free checks of installed clients in an exact candidate image."""
import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "images" / "trust"))
from verify import HOSTS
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qualification import chains

ISSUER_ERRORS = (
    "certificate signed by unknown authority",
    "certificate signed by a compromised authority",
    "certificate is not trusted",
    "server certificate verification failed",
    "unable to get local issuer certificate",
    "unable to verify the first certificate",
    "self-signed certificate in certificate chain",
    "invalid peer certificate: unknownissuer",
    "invalid peer certificate: certdistrusted",
    "invalid peer certificate: other(othererror(certdistrusted))",
)
DUMMY_TOKEN = "public-tls-probe-not-a-secret"
COMMAND_TIMEOUT = 60
QUALIFICATION_TIMEOUT = 18 * 60
EXPIRY_WARNING = 60 * 24 * 60 * 60
VERSION_COMMANDS = {
    "git": ["git", "--version"], "gh": ["gh", "--version"],
    "ghtoken": ["ghtoken", "--version"],
    "gcloud": ["/root/google-cloud-sdk/bin/gcloud", "--version"],
    "buf": ["buf", "--version"], "cargo-audit": ["cargo", "audit", "--version"],
    "cr": ["cr", "version"], "helm": ["helm", "version", "--short"],
    "kubectl": ["kubectl", "version", "--client=true", "--short"],
    "certifi": [sys.executable, "-m", "pip", "show", "certifi"],
    "pip": [sys.executable, "-m", "pip", "--version"],
}


class QualificationDeadline(Exception):
    """Leave a structured failure before the outer CI timeout."""


def environment(home):
    # Never forward runner credentials, proxies, custom roots or user config.
    return {
        "PATH": "/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home), "LC_ALL": "C", "GH_TOKEN": DUMMY_TOKEN,
        "PIP_CONFIG_FILE": "/dev/null", "KUBECONFIG": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "CARGO_HOME": "/usr/local/cargo", "RUSTUP_HOME": "/usr/local/rustup",
        "CLOUDSDK_CONFIG": str(home / "gcloud"),
        "CLOUDSDK_AUTH_ACCESS_TOKEN": DUMMY_TOKEN,
        "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true",
        "CLOUDSDK_STORAGE_MAX_RETRIES": "0",
    }


def execute(args, env, cwd, *, stderr_only=False, timeout=COMMAND_TIMEOUT):
    try:
        result = subprocess.run(args, env=env, cwd=cwd, capture_output=True,
                                text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, type(error).__name__
    return result.returncode, result.stderr if stderr_only else result.stdout + result.stderr


def accepted(code, output, *, negative=False, auth_control=None):
    if code is None:
        return False
    if negative:
        # gh-token v2.0.1 prints errors to stdout. Its non-200 path prints only
        # a status code, never the response body; require its local error prefix.
        if auth_control == "ghtoken" and not output.startswith(
                "Error: failed listing installations: unable to POST to https://"):
            return False
        message = output.lower()
        unrelated = ("certificate has expired", "certificate is expired", "expired certificate", "not yet valid",
                     "hostname mismatch", "doesn't match", "does not match",
                     "no alternative certificate subject name")
        http_response = re.search(r"\bhttp(?:error)?(?:/\d(?:\.\d)?)?[ :]+[1-5]\d\d\b", message)
        return (code != 0 and not http_response and not any(error in message for error in unrelated)
                and any(error in message for error in ISSUER_ERRORS))
    # gh requires a token even for its public API command. A deliberately
    # invalid token's HTTP 401 proves TLS only, never authenticated API access.
    if auth_control == "gh":
        return code != 0 and "HTTP 401" in output and "Bad credentials" in output
    if auth_control == "gcloud":
        return code != 0 and "HTTPError 401" in output and "Invalid Credentials" in output
    if auth_control == "ghtoken":
        return code != 0 and output.strip() in (
            "Error: failed listing installations: unexpected status code: 401",
            "Error: failed listing installations: unexpected status code: 404",
        )
    if auth_control == "cr" and code != 0:
        # cr also prints progress and Cobra usage. Require one complete quota
        # error, and reject any additional failure instead of matching one line
        # inside a mixed diagnostic (including errors appended on that line).
        errors = [line.strip() for line in output.splitlines() if re.search(
            r"\b(?:errors?|failed|failure|fatal|panic|exception|traceback|timeout|"
            r"TLS|SSL|certificate|DNS|connection)\b|timed out|no such host|HTTP(?:/[0-9.]+)? [45][0-9]{2}",
            line, re.I)]
        return len(errors) == 1 and bool(re.fullmatch(
            r"Error: GET https://api\.github\.com/repos/helm/chart-releaser/releases/tags/v1\.6\.0: "
            r"403 API rate limit exceeded"
            r"(?: \[rate reset in [-0-9.hmsµ]+\])?", errors[0]))
    return code == 0


def negative_probe(client, host, command, expected, peer, env, home, run=None):
    run = run or execute
    before = chains.snapshot(host, peer, expected, run, env, home)
    code, output, after = None, "Unexpected or unavailable published chain", None
    if before["matches"]:
        code, output = run(command, env, home, stderr_only=client != "ghtoken")
        after = chains.snapshot(host, peer, expected, run, env, home)
    distrusted = bool(before["matches"] and after["matches"] and
                      accepted(code, output, negative=True, auth_control=client))
    return {"host": host, "exit": code, "diagnostic": output[-600:],
            "chain_before": before, "chain_after": after, "distrusted": distrusted}


def commands(profile, home):
    clients = {
        "git": (lambda host: ["git", "ls-remote", f"https://{host}/probe.git"],
                ["git", "ls-remote", "https://github.com/blinkbitcoin/concourse-shared.git", "HEAD"]),
    }
    if profile != "wincross":
        clients["gh"] = (lambda host: ["gh", "api", "--hostname", host, "/"],
                         ["gh", "api", "https://api.github.com/user"])
        cloud = ["/root/google-cloud-sdk/bin/gcloud", "storage", "buckets", "describe",
                 "gs://candidate-tls-probe", "--format=json", "--quiet"]
        clients["gcloud"] = (lambda host: ["env", "CLOUDSDK_API_ENDPOINT_OVERRIDES_STORAGE="
                                          + f"https://{host}/"] + cloud, cloud)
        token = ["ghtoken", "installations", "--key", str(home / "probe-key.pem"), "--app-id", "1"]
        clients["ghtoken"] = (lambda host: token + ["--hostname", host], token)
    if profile in ("node", "rust"):
        clients["buf"] = (lambda host: ["buf", "ls-files", f"https://{host}/probe.tar.gz"],
                          ["buf", "ls-files", "https://github.com/protocolbuffers/protobuf/archive/refs/tags/"
                           "v3.20.3.tar.gz#strip_components=1,subdir=src/google/protobuf"])
    if profile == "rust":
        audit = ["cargo", "audit", "--file", str(home / "Cargo.lock"), "--no-yanked"]
        clients["cargo-audit"] = (
            lambda host: audit + ["--db", str(home / ("advisories-" + host)), "--url", f"https://{host}/probe.git"],
            audit + ["--db", str(home / "healthy-advisories"), "--url", "https://github.com/RustSec/advisory-db.git"],
        )
    if profile == "release":
        chart = ["cr", "index", "--owner", "helm", "--git-repo", "chart-releaser",
                 "--release-name-template", "v1.6.0", "--package-path", str(home / "chart-releaser/packages"),
                 "--index-path", str(home / "chart-releaser/index.yaml")]
        clients["cr"] = (lambda host: chart + ["--git-base-url", f"https://{host}/"], chart)
        clients.update({
            "helm": (lambda host: ["helm", "repo", "add", "negative-" + host, f"https://{host}"],
                     ["helm", "repo", "add", "healthy", "https://charts.helm.sh/stable"]),
            "kubectl": (lambda host: ["kubectl", "--token=" + DUMMY_TOKEN,
                        "--server=https://" + host, "--request-timeout=30s", "get", "--raw=/"],
                        ["kubectl", "--token=" + DUMMY_TOKEN, "--server=https://kubernetes.io",
                         "--request-timeout=30s", "get", "--raw=/"]),
        })
        prefix = [sys.executable, str(Path(__file__).resolve()), "certifi-request"]
        clients["certifi"] = (lambda host: prefix + [f"https://{host}/"],
                              prefix + ["https://pypi.org/simple/pip/"])
        pip = [sys.executable, "-m", "pip", "download", "--no-deps", "--no-cache-dir",
               "--only-binary=:all:", "--disable-pip-version-check", "--no-input",
               "--retries", "0", "--timeout", "10"]
        clients["pip"] = (
            lambda host: pip + ["--dest", str(home / ("pip-" + host)), "--index-url",
                                f"https://{host}/simple/", "candidate-probe==0"],
            pip + ["--dest", str(home / "pip-healthy"), "--index-url",
                   "https://pypi.org/simple/", "pip==26.2.2"],
        )
    return clients


def certifi_request(url):
    import certifi
    import ssl
    import urllib.request
    with urllib.request.urlopen(url, context=ssl.create_default_context(
            cafile=certifi.where()), timeout=30) as response:
        return response.status


def prepare_chart_probe(home, env, run=None):
    run = run or execute
    directory = home / "chart-releaser"
    directory.mkdir()
    # Clone a public fixture; never create or push a release/PR/commit.
    for command in (
        ["git", "clone", "--depth=1", "--branch", "v1.6.0", "https://github.com/helm/chart-releaser.git", "."],
        ["git", "update-ref", "refs/remotes/origin/gh-pages", "HEAD"],
    ):
        code, _ = run(command, env, directory)
        if code != 0:
            return False
    packages = directory / "packages"
    packages.mkdir()
    chart = b"apiVersion: v2\nname: candidate-probe\nversion: 0.1.0\n"
    with tarfile.open(packages / "candidate-probe-0.1.0.tgz", "w:gz") as archive:
        member = tarfile.TarInfo("candidate-probe/Chart.yaml")
        member.size = len(chart)
        archive.addfile(member, io.BytesIO(chart))
    return True


def qualify(profile, home):
    deadline = time.monotonic() + QUALIFICATION_TIMEOUT

    def run(args, env, cwd, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QualificationDeadline()
        result = execute(args, env, cwd, timeout=min(COMMAND_TIMEOUT, remaining), **kwargs)
        if time.monotonic() >= deadline:
            raise QualificationDeadline()
        return result

    try:
        return qualify_clients(profile, home, run)
    except QualificationDeadline:
        print(json.dumps({"aborted": "qualification-deadline", "passed": False}), flush=True)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(json.dumps({"setup": "qualification-inputs", "error": type(error).__name__,
                          "passed": False}), flush=True)
    return False


def qualify_clients(profile, home, run):
    env = environment(home)
    expected, peers = chains.published_chains(), chains.pinned_peers()
    for host in HOSTS:
        observed = chains.snapshot(host, peers[host], expected[host], run, env, home,
                                   minimum_validity=EXPIRY_WARNING)
        if not observed["matches"]:
            print(json.dumps({"setup": "chain-preflight", "host": host, "chain": observed,
                              "passed": False}), flush=True)
            return False
    if profile == "release" and not prepare_chart_probe(home, env, run):
        print(json.dumps({"setup": "chart-index-fixture", "passed": False}), flush=True)
        return False
    if profile == "rust":
        (home / "Cargo.lock").write_text('version = 4\n[[package]]\nname = "candidate-probe"\nversion = "0.1.0"\n')
    if profile != "wincross":
        # This disposable key belongs to no GitHub App. Never use operator keys.
        code, _ = run(["openssl", "genrsa", "-out", str(home / "probe-key.pem"), "2048"], env, home)
        if code != 0:
            print(json.dumps({"setup": "synthetic-app-key", "passed": False}), flush=True)
            return False
    passed = True
    for client, (negative_command, healthy_command) in commands(profile, home).items():
        version_command = VERSION_COMMANDS[client]
        working = home / "chart-releaser" if client == "cr" else home
        version_code, version = run(version_command, env, working)
        results = []
        for host in HOSTS:
            results.append(negative_probe(client, host, negative_command(host),
                                          expected[host], peers[host], env, working, run))
            probe = results[-1]
            if not probe["chain_before"]["matches"] or not probe["chain_after"]["matches"]:
                print(json.dumps({"aborted": "chain-drift", "client": client,
                                  "probe": probe, "passed": False}), flush=True)
                return False
        code, output = run(healthy_command, env, working)
        healthy = accepted(code, output, auth_control=client)
        good = version_code == 0 and all(row["distrusted"] for row in results) and healthy
        passed = passed and good
        print(json.dumps({"client": client, "version": version[:300],
                          "version_exit": version_code,
                          "negative": results, "healthy_tls": healthy,
                          "healthy_exit": code, "healthy_diagnostic": output[:400],
                          "healthy_authenticated": False,
                          "healthy_operation_succeeded": code == 0, "passed": good}), flush=True)
    return passed


def smoke(profile, home):
    env = environment(home)
    if profile in ("rust", "wincross"):
        source = home / "main.rs"
        source.write_text('fn main() { println!("candidate smoke"); }\n')
        args = ["rustc", str(source), "-o", str(home / "smoke")]
        if profile == "wincross":
            args += ["--target", "x86_64-pc-windows-gnu"]
    elif profile == "node":
        args = ["node", "--eval", 'require("node:assert").equal(2 + 2, 4)']
    else:
        args = [sys.executable, "-m", "pip", "check"]
    code, _ = execute(args, env, home)
    print(json.dumps({"tooling_smoke": profile, "passed": code == 0}), flush=True)
    return code == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    probe = sub.add_parser("qualify")
    probe.add_argument("profile", choices=("node", "rust", "release", "wincross"))
    request = sub.add_parser("certifi-request")
    request.add_argument("url")
    args = parser.parse_args(argv)
    if args.mode == "certifi-request":
        status = certifi_request(args.url)
        return 0 if status == 200 else 1
    with tempfile.TemporaryDirectory(prefix="candidate-client-") as home:
        clients_pass = qualify(args.profile, Path(home))
        smoke_pass = smoke(args.profile, Path(home))
        return 0 if clients_pass and smoke_pass else 1


if __name__ == "__main__":
    sys.exit(main())
