#!/usr/bin/env bash
# End-to-end smoke test of the orphan cleanup runbook in the exact pinned
# Kaniko task image (read from ci/pipeline.yml). Requires Docker on Linux x86-64.
#
# The process layout matches a Concourse task container: PID 1 is an idle init
# (Concourse runs gdn-init), and both the task's /kaniko/executor and every
# intercept are exec'd into the container from outside its PID namespace, so
# their parent PID reads as 0. A real build blocks in a RUN step; staging and
# cleanup then run as separate execs with the documented script, stdin stream,
# expected digest and helper invocation.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
container=
cleanup() {
  [ -z "$container" ] || docker rm -f "$container" > /dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

image=$(python3 - "$root/ci/pipeline.yml" <<'EOF'
import re, sys
text = open(sys.argv[1]).read()
match = re.search(r"repository: (gcr\.io/kaniko-project/executor)\n\s+tag: \S+\n\s+version:\n"
                  r"\s+digest: (sha256:[0-9a-f]{64})", text)
if not match:
    raise SystemExit("pinned Kaniko task image not found in ci/pipeline.yml")
print(f"{match[1]}@{match[2]}")
EOF
)
read -r url upstream_sha256 sha256 < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["upstream_url"], m["upstream_sha256"], m["runtime_sha256"])' \
  "$root/ci/ops/cleanup-runtime.json")
stage=$(cat "$root/ci/ops/stage-cleanup-runtime.sh")
helper=$(cat "$root/ci/ops/stop-orphaned-image-build.py")
runtime=/kaniko/orphan-cleanup-runtime/python/bin/python3

echo "Kaniko task image: $image"
curl -fsSL "$url" -o "$work/upstream.tar.zst"
echo "$upstream_sha256  $work/upstream.tar.zst" | sha256sum -c -
# Rebuilding must reproduce the pinned runtime digest exactly.
python3 "$root/ci/ops/build-cleanup-runtime.py" "$work/upstream.tar.zst" "$work/runtime.tar"

# A base image the workflow already pins, so the build has a real RUN step.
mkdir "$work/context"
printf 'FROM python@sha256:18d373e126e39c29c6d38e71e2f19ca66636568318fc95c7849c462b7f9d3d64\nRUN sleep 300\n' \
  > "$work/context/Dockerfile"

# PID 1 stands in for gdn-init: it starts nothing and is never a target.
container=$(docker run -d --platform linux/amd64 --entrypoint /busybox/sleep \
  -v "$work/context:/workspace/context:ro" -w /workspace "$image" 3600)

in_container() { docker exec -i "$container" "$@"; }

# Like the Concourse task, exec the executor directly (parent 0). Kaniko wipes
# everything except /kaniko, /busybox and mounts before unpacking the base
# image, so the output paths live under /kaniko.
docker exec -d -w /workspace "$container" /kaniko/executor \
  --dockerfile=context/Dockerfile --context=context \
  --use-new-run --single-snapshot --cache=false --no-push \
  --tar-path=/kaniko/smoke-image.tar --digest-file=/kaniko/smoke-digest

echo "Negative: a wrong digest must fail and leave nothing staged"
wrong=$(printf '%064d' 0)
if in_container /busybox/sh -c "$stage" stage "$wrong" < "$work/runtime.tar"; then
  echo "staging accepted a wrong digest" >&2; exit 1
fi
in_container /busybox/sh -c '[ ! -e /kaniko/orphan-cleanup-runtime ]'

echo "Negative: a malformed digest must be rejected before reading stdin"
if in_container /busybox/sh -c "$stage" stage not-a-digest < /dev/null; then
  echo "staging accepted a malformed digest" >&2; exit 1
fi

echo "Stage the verified runtime"
in_container /busybox/sh -c "$stage" stage "$sha256" < "$work/runtime.tar"

echo "Negative: restaging over an existing runtime is refused"
if in_container /busybox/sh -c "$stage" stage "$sha256" < "$work/runtime.tar"; then
  echo "staging replaced an existing runtime" >&2; exit 1
fi

echo "Wait for the Kaniko RUN step"
for _ in $(seq 1 180); do
  if in_container "$runtime" -I -c '
import pathlib, sys
sys.exit(0 if any(p.read_bytes() == b"sleep\x00300\x00" for p in pathlib.Path("/proc").glob("[0-9]*/cmdline")) else 1)
' 2> /dev/null; then
    running=1
    break
  fi
  sleep 2
done
if [ -z "${running:-}" ]; then
  echo "Kaniko RUN step did not start" >&2
  exit 1
fi

# Record the operator identity evidence for the executor, as the runbook does.
args=$(in_container "$runtime" -I -c '
import hashlib, os, pathlib
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command = (entry / "cmdline").read_bytes()
    except FileNotFoundError:
        continue
    if command.split(b"\0")[0] == b"/kaniko/executor":
        raw = (entry / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        # Production layout: the task process is an exec, so its parent is 0.
        assert fields[1] == "0", f"executor parent is {fields[1]}, not 0"
        print(entry.name, fields[19], hashlib.sha256(command).hexdigest(), os.readlink(entry / "cwd"))
')
read -r pid start command_sha256 cwd <<< "$args"
[ -n "${pid:-}" ] || { echo "executor not found" >&2; exit 1; }
echo "Executor identity: $args"

# The documented invocation: the helper's exact source as -c in its own exec,
# with no other exec running concurrently.
set +e
result=$(in_container "$runtime" -I -c "$helper" --apply "$pid" "$start" "$command_sha256" "$cwd" 2>&1)
rc=$?
set -e
echo "$result"
[ "$rc" = 0 ] || { echo "helper failed with exit code $rc" >&2; exit 1; }
python3 -c '
import json, sys
receipt = json.loads(sys.argv[1].strip().splitlines()[-1])
assert receipt["stopped_processes"] >= 2, receipt
print("receipt", receipt)
' "$result"

echo "Verify: no live process remains except PID 1 and this check"
in_container "$runtime" -I -c '
import os, pathlib, sys
live = []
for entry in pathlib.Path("/proc").iterdir():
    if entry.name.isdigit() and int(entry.name) not in (1, os.getpid()):
        try:
            raw = (entry / "stat").read_text()
            if raw[raw.rfind(")") + 2:].split()[0] != "Z":
                live.append((entry.name, (entry / "cmdline").read_bytes()))
        except FileNotFoundError:
            continue  # Exited during the scan.
sys.exit(f"live processes remain: {live}" if live else 0)
'
echo "Orphan cleanup smoke test passed"
