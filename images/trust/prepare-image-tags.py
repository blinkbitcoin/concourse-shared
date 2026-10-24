"""Prepare a content tag outside Kaniko's disposable build filesystem."""
from pathlib import Path
import re
import sys


def prepare(digest_file, tags_file):
    digest = Path(digest_file).read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("invalid image manifest digest")
    Path(tags_file).write_text("sha256-" + digest.removeprefix("sha256:") + "\n")
    print(digest)


if __name__ == "__main__":
    prepare(*sys.argv[1:])
