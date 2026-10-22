#!/usr/bin/env python3
"""Qualify Debian 12 system trust before building shared CI images."""
import argparse
import os
from pathlib import Path
import subprocess

MINIMUM = "20250419~deb12u2"
BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
HOSTS = tuple(
    f"{kind}-g3{letter}{cross}-emudhra.we-have-a-problem.com"
    for kind in ("dv", "ov", "ev")
    for letter in ("b", "c")
    for cross in ("", "-aaa")
)
ISSUER_ERRORS = (
    "unable to get local issuer certificate",
    "unable to verify the first certificate",
    "self-signed certificate in certificate chain",
)
NODE_ERRORS = (
    "CERT_REVOKED", "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
    "UNABLE_TO_VERIFY_LEAF_SIGNATURE", "SELF_SIGNED_CERT_IN_CHAIN",
)
NODE_PROBE = """
require('node:https').get(process.argv[1], {signal: AbortSignal.timeout(30000)},
  response => { response.resume(); process.exit(0); }
).on('error', error => { console.error(error.code); process.exit(1); });
"""


def run(*args):
    # Do not inherit proxy/CA overrides from a build or an operator shell.
    return subprocess.run(
        args, capture_output=True, text=True, timeout=900,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
             "DEBIAN_FRONTEND": "noninteractive"},
    )


def require(*args):
    result = run(*args)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed; image build remains blocked")
    return result.stdout.strip()


def qualify(refresh=False, node=False, rustup=False):
    release = Path("/etc/os-release").read_text().splitlines()
    if "ID=debian" not in release or 'VERSION_ID="12"' not in release:
        raise RuntimeError("Only Debian 12 is qualified")
    if refresh:
        if os.geteuid() != 0:
            raise RuntimeError("Package refresh requires root")
        require("apt-get", "-o", "APT::Update::Error-Mode=any", "update")
        require("apt-get", "-y", "--no-remove", "--no-install-recommends",
                "install", "ca-certificates", "curl")
        require("update-ca-certificates")
    package = require("dpkg-query", "-W", "-f=${Status}\n${Version}", "ca-certificates")
    lines = package.splitlines()
    if len(lines) != 2 or lines[0] != "install ok installed":
        raise RuntimeError("CA package is not fully installed")
    require("dpkg", "--compare-versions", lines[1], "ge", MINIMUM)
    if not Path(BUNDLE).is_file():
        raise RuntimeError("System CA bundle is missing")
    curl = ("/usr/bin/curl", "-q", "--noproxy", "*", "--cacert", BUNDLE,
            "--capath", "/etc/ssl/certs", "--connect-timeout", "10",
            "--max-time", "30", "--silent", "--show-error", "--fail",
            "--output", "/dev/null")
    for host in HOSTS:
        result = run(*curl, f"https://{host}/")
        if result.returncode != 60 or not any(error in result.stderr for error in ISSUER_ERRORS):
            raise RuntimeError(f"Inconclusive or accepted chain: {host}")
    require(*curl, "https://deb.debian.org/")
    print(f"Debian system trust qualified: ca-certificates={lines[1]}, rejected=12, healthy=pass")
    if node:
        probe = ("/usr/local/bin/node", "--eval", NODE_PROBE)
        for host in HOSTS:
            result = run(*probe, f"https://{host}/")
            if result.returncode != 1 or result.stderr.strip() not in NODE_ERRORS:
                raise RuntimeError(f"Node inconclusive or accepted chain: {host}")
        require(*probe, "https://registry.npmjs.org/")
        print("Node bundled trust qualified: rejected=12, healthy=pass")
    if rustup:
        environment = ("/usr/bin/env", "HOME=/root", "RUSTUP_HOME=/usr/local/rustup",
                       "CARGO_HOME=/usr/local/cargo")
        command = ("/usr/local/cargo/bin/rustup", "check")
        for host in HOSTS:
            result = run(*environment, f"RUSTUP_DIST_SERVER=https://{host}",
                         f"RUSTUP_UPDATE_ROOT=https://{host}", *command)
            if result.returncode != 1 or "CertDistrusted" not in result.stderr:
                raise RuntimeError(f"Rustup inconclusive or accepted chain: {host}")
        require(*environment, *command)
        print("Rustup verifier qualified: rejected=12, healthy=pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("verify", "refresh"))
    parser.add_argument("--node", action="store_true", help="Also qualify Node's bundled verifier")
    parser.add_argument("--rustup", action="store_true", help="Also qualify Rustup's bundled verifier")
    args = parser.parse_args()
    try:
        qualify(refresh=args.mode == "refresh", node=args.node, rustup=args.rustup)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Trust gate failed: {error}\n")


if __name__ == "__main__":
    main()
