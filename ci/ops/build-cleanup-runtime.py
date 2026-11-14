"""Build the pinned orphan-cleanup runtime archive from its upstream release.

The retained Kaniko task container has BusyBox (tar, sha256sum) but no Python
and no zstd. Upstream publishes the fully static musl CPython only as
.tar.zst, so this repacks the interpreter and standard library into a plain
tar with fixed metadata. The output is reproducible: its SHA256 must equal the
pinned runtime_sha256, which stage-cleanup-runtime.sh verifies again in the
container.

usage: build-cleanup-runtime.py UPSTREAM.tar.zst OUTPUT.tar
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

PIN = Path(__file__).with_name("cleanup-runtime.json")
SOURCE = "python/install/"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected(name, version):
    """Return the runtime path for an upstream file, or None to drop it."""
    if not name.startswith(SOURCE):
        return None
    relative = name[len(SOURCE):]
    if relative in (f"bin/python{version}", "bin/python3"):
        return "python/" + relative
    library = f"lib/python{version}/"
    if relative.startswith(library):
        top = relative[len(library):].split("/")[0]
        if top == "test" or top.startswith("config-"):
            return None  # Test suite and static build/link inputs.
        return "python/" + relative
    return None


def parents(name):
    parts = name.split("/")[:-1]
    return {"/".join(parts[:index]) for index in range(1, len(parts) + 1)}


def repack(source, output, version):
    files = {}
    for member in source:
        if member.isdir():
            continue  # Directories are synthesized below with fixed metadata.
        name = selected(member.name, version)
        if name is None:
            continue
        if not (member.isfile() or member.issym()):
            raise RuntimeError(f"unexpected member type: {member.name}")
        files[name] = (member, source.extractfile(member).read() if member.isfile() else None)
    for required in (f"python/bin/python{version}", "python/bin/python3",
                     f"python/lib/python{version}/os.py"):
        if required not in files:
            raise RuntimeError(f"upstream archive is missing {required}")
    directories = set().union(*(parents(name) for name in files))
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as target:
        for name in sorted(directories | set(files)):
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            data = None
            if name in directories:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            elif files[name][0].issym():
                info.type, info.mode = tarfile.SYMTYPE, 0o777
                info.linkname = files[name][0].linkname
            else:
                member, data = files[name]
                info.mode = 0o755 if member.mode & 0o111 else 0o644
                info.size = len(data)
            target.addfile(info, io.BytesIO(data) if data is not None else None)


def build(upstream, output, pin=None):
    pin = pin or json.loads(PIN.read_text())
    if sha256(upstream) != pin["upstream_sha256"]:
        raise RuntimeError("upstream archive does not match upstream_sha256")
    version = ".".join(pin["python"].split(".")[:2])
    with subprocess.Popen(["zstd", "-dc", str(upstream)], stdout=subprocess.PIPE) as zstd:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as source, \
                open(output, "wb") as target:
            repack(source, target, version)
    if zstd.returncode != 0:
        raise RuntimeError("zstd failed to decompress the upstream archive")
    if sha256(output) != pin["runtime_sha256"]:
        Path(output).unlink()  # Never leave an unverified runtime behind.
        raise RuntimeError("repacked runtime does not match runtime_sha256")
    return pin["runtime_sha256"]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: build-cleanup-runtime.py UPSTREAM.tar.zst OUTPUT.tar")
    print(build(Path(sys.argv[1]), Path(sys.argv[2])))
