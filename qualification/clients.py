#!/usr/bin/env python3
"""Credential-free checks of installed clients in an exact candidate image."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "images" / "trust"))
from verify import HOSTS

ISSUER_ERRORS = (
    "certificate signed by unknown authority",
    "certificate signed by a compromised authority",
    "certificate is not trusted",
    "server certificate verification failed",
    "unable to get local issuer certificate",
    "unable to verify the first certificate",
    "self-signed certificate in certificate chain",
)
DUMMY_TOKEN = "public-tls-probe-not-a-secret"


def environment(home):
    # Never forward runner credentials, proxies, custom roots or user config.
    return {
        "PATH": "/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home), "LC_ALL": "C", "GH_TOKEN": DUMMY_TOKEN,
        "KUBECONFIG": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "CARGO_HOME": "/usr/local/cargo", "RUSTUP_HOME": "/usr/local/rustup",
    }


def execute(args, env, cwd):
    try:
        result = subprocess.run(args, env=env, cwd=cwd, capture_output=True,
                                text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, type(error).__name__
    return result.returncode, result.stdout + result.stderr


def accepted(code, output, *, negative=False, gh_control=False):
    if code is None:
        return False
    if negative:
        message = output.lower()
        unrelated = ("expired", "not yet valid", "hostname", "certificate subject name")
        return (code != 0 and not any(error in message for error in unrelated)
                and any(error in message for error in ISSUER_ERRORS))
    # gh requires a token even for its public API command. A deliberately
    # invalid token's HTTP 401 proves TLS only, never authenticated API access.
    if gh_control:
        return code != 0 and "HTTP 401" in output and "Bad credentials" in output
    return code == 0


def commands(profile):
    clients = {
        "git": (lambda host: ["git", "ls-remote", f"https://{host}/probe.git"],
                ["git", "ls-remote", "https://github.com/blinkbitcoin/concourse-shared.git", "HEAD"]),
    }
    if profile != "wincross":
        clients["gh"] = (lambda host: ["gh", "api", "--hostname", host, "/"],
                         ["gh", "api", "https://api.github.com/user"])
    if profile == "release":
        clients.update({
            "helm": (lambda host: ["helm", "repo", "add", "negative", f"https://{host}"],
                     ["helm", "repo", "add", "healthy", "https://charts.helm.sh/stable"]),
            "kubectl": (lambda host: ["kubectl", "--token=" + DUMMY_TOKEN,
                        "--server=https://" + host, "--request-timeout=30s", "get", "--raw=/"],
                        ["kubectl", "--token=" + DUMMY_TOKEN, "--server=https://kubernetes.io",
                         "--request-timeout=30s", "get", "--raw=/"]),
        })
        for backend in ("certifi", "pip"):
            prefix = [sys.executable, str(Path(__file__).resolve()), "python-request", backend]
            clients[backend] = (lambda host, p=prefix: p + [f"https://{host}/"],
                                prefix + ["https://pypi.org/simple/pip/"])
    return clients


def python_request(backend, url):
    if backend == "certifi":
        import certifi
        import ssl
        import urllib.request
        with urllib.request.urlopen(url, context=ssl.create_default_context(
                cafile=certifi.where()), timeout=30) as response:
            return response.status
    from pip._vendor import requests
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.status_code


def qualify(profile, home):
    env = environment(home)
    passed = True
    for client, (negative_command, healthy_command) in commands(profile).items():
        version_command = {
            "git": ["git", "--version"], "gh": ["gh", "--version"],
            "helm": ["helm", "version", "--short"],
            "kubectl": ["kubectl", "version", "--client=true", "--short"],
            "certifi": [sys.executable, "-m", "pip", "show", "certifi"],
            "pip": [sys.executable, "-m", "pip", "--version"],
        }[client]
        version_code, version = execute(version_command, env, home)
        results = []
        for host in HOSTS:
            code, output = execute(negative_command(host), env, home)
            results.append({"host": host, "exit": code, "diagnostic": output[:400],
                            "distrusted": accepted(code, output, negative=True)})
        code, output = execute(healthy_command, env, home)
        healthy = accepted(code, output, gh_control=client == "gh")
        good = version_code == 0 and all(row["distrusted"] for row in results) and healthy
        passed = passed and good
        print(json.dumps({"client": client, "version": version[:300],
                          "version_exit": version_code,
                          "negative": results, "healthy_tls": healthy,
                          "healthy_exit": code, "healthy_diagnostic": output[:400],
                          "healthy_authenticated": False, "passed": good}), flush=True)
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
        args = ["python3", "-m", "pip", "check"]
    code, _ = execute(args, env, home)
    print(json.dumps({"tooling_smoke": profile, "passed": code == 0}), flush=True)
    return code == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    probe = sub.add_parser("qualify")
    probe.add_argument("profile", choices=("node", "rust", "release", "wincross"))
    request = sub.add_parser("python-request")
    request.add_argument("backend", choices=("certifi", "pip"))
    request.add_argument("url")
    args = parser.parse_args(argv)
    if args.mode == "python-request":
        status = python_request(args.backend, args.url)
        return 0 if status == 200 else 1
    with tempfile.TemporaryDirectory(prefix="candidate-client-") as home:
        clients_pass = qualify(args.profile, Path(home))
        smoke_pass = smoke(args.profile, Path(home))
        return 0 if clients_pass and smoke_pass else 1


if __name__ == "__main__":
    sys.exit(main())
