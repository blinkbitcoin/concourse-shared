"""Bind live candidate probes to the checksum-pinned published chains."""
import base64
import hashlib
import io
import ipaddress
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "images" / "trust"))
from verify import FIXTURE_SHA256, FIXTURE_ZIP, HOSTS


def fingerprints(pem):
    certificates = re.findall(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", pem, re.S)
    return [hashlib.sha256(base64.b64decode("".join(c.split()), validate=True)).hexdigest()
            for c in certificates]


def published_chains():
    archive = base64.b64decode(FIXTURE_ZIP, validate=True)
    if hashlib.sha256(archive).hexdigest() != FIXTURE_SHA256:
        raise ValueError("Published chain fixture checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as fixtures:
        expected = {host: fingerprints(fixtures.read(f"chains/{host}.pem").decode()) for host in HOSTS}
    if any(len(chain) < 2 for chain in expected.values()):
        raise ValueError("Incomplete published certificate chain")
    return expected


def pinned_peers(hosts_file=Path("/etc/hosts")):
    peers = {}
    for line in hosts_file.read_text().splitlines():
        fields = line.split("#", 1)[0].split()
        for host in set(fields[1:]).intersection(HOSTS):
            if host in peers:
                raise ValueError(f"Duplicate peer pin: {host}")
            peers[host] = str(ipaddress.IPv4Address(fields[0]))
    if set(peers) != set(HOSTS):
        raise ValueError("Run inside the disposable container with every --add-host pin")
    return peers


def snapshot(host, peer, expected, execute, env, home, *, minimum_validity=0):
    code, output = execute(["openssl", "s_client", "-connect", f"{peer}:443",
                            "-servername", host, "-showcerts"], env, home)
    try:
        actual = fingerprints(output)
    except ValueError:
        actual = []
    result = {"peer": peer, "expected_sha256": expected, "observed_sha256": actual,
              "valid_now": False, "matches": False, "reason": "chain-unavailable-or-mismatched"}
    if code != 0 or actual != expected or len(actual) < 2:
        return result
    # Trust the observed final certificate only for this integrity control.
    # Never add it to a client's trust store. Omit -attime for the current-time
    # check: signatures, purpose, hostname and validity must all succeed now.
    certificates = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", output, re.S)
    with tempfile.TemporaryDirectory(dir=home) as directory:
        chain, anchor = Path(directory) / "chain.pem", Path(directory) / "anchor.pem"
        chain.write_text("\n".join(certificates))
        anchor.write_text(certificates[-1])
        args = ["openssl", "verify", "-trusted", str(anchor), "-partial_chain",
                "-no-CAfile", "-no-CApath", "-no-CAstore", "-purpose", "sslserver",
                "-verify_hostname", host, "-untrusted", str(chain)]
        code, diagnostic = execute(args + [str(chain)], env, home)
        result.update(valid_now=code == 0, reason="chain-invalid-now", validity_diagnostic=diagnostic[-600:])
        if code != 0:
            return result
        if minimum_validity:
            code, diagnostic = execute(args + ["-attime", str(int(time.time()) + minimum_validity),
                                                str(chain)], env, home)
            result.update(minimum_validity_seconds=minimum_validity,
                          validity_diagnostic=diagnostic[-600:])
            if code != 0:
                result["reason"] = "chain-expires-within-warning-window"
                return result
    result.update(matches=True, reason="valid-published-chain")
    return result


def docker_arguments():
    # Only the disposable candidate container gets these host entries. Its
    # clients and independent chain snapshots use the same fixed IPv4 peer.
    return [f"--add-host={host}:{ipaddress.IPv4Address(socket.gethostbyname(host))}" for host in HOSTS]


if __name__ == "__main__":
    print("\n".join(docker_arguments()))
