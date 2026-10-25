"""Bind live candidate probes to the checksum-pinned published chains."""
import base64
import hashlib
import io
import ipaddress
from pathlib import Path
import re
import socket
import sys
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


def snapshot(host, peer, expected, execute, env, home):
    code, output = execute(["openssl", "s_client", "-connect", f"{peer}:443",
                            "-servername", host, "-showcerts"], env, home)
    try:
        actual = fingerprints(output)
    except ValueError:
        actual = []
    return {"peer": peer, "expected_sha256": expected, "observed_sha256": actual,
            "matches": code == 0 and actual == expected}


def docker_arguments():
    # Only the disposable candidate container gets these host entries. Its
    # clients and independent chain snapshots use the same fixed IPv4 peer.
    return [f"--add-host={host}:{ipaddress.IPv4Address(socket.gethostbyname(host))}" for host in HOSTS]


if __name__ == "__main__":
    print("\n".join(docker_arguments()))
