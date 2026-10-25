"""Allow only the reviewed transport delta from Cargo Audit's release lock."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tomllib


def canonical(lock):
    # Package ordering is immaterial; preserve every record and metadata field.
    return json.dumps({**lock, "package": sorted(
        lock["package"], key=lambda package: json.dumps(package, sort_keys=True))},
        sort_keys=True).encode()


def verify(baseline, proposed, delta):
    if hashlib.sha256(canonical(baseline)).hexdigest() != delta["baseline_sha256"]:
        raise ValueError("Published Cargo Audit baseline does not match the reviewed snapshot")
    expected = copy.deepcopy(baseline)
    for package in delta["remove"]:
        expected["package"].remove(package)
    expected["package"].extend(delta["add"])
    if canonical(proposed) != canonical(expected):
        raise ValueError("Cargo.lock differs from the reviewed transport delta")


def main():
    baseline, proposed = (tomllib.loads(Path(path).read_text()) for path in sys.argv[1:])
    delta = json.loads(Path(__file__).with_name("lock-delta.json").read_text())
    verify(baseline, proposed, delta)
    print("Cargo Audit lock preserves the published snapshot plus the reviewed transport delta")


if __name__ == "__main__":
    main()
