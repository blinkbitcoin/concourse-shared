#!/usr/bin/env bash
# End-to-end smoke test of the orphan cleanup runbook in the exact pinned
# Kaniko task image (read from ci/pipeline.yml). Requires Docker on Linux x86-64.
#
# The container's PID 1 is a BusyBox shell standing in for the task supervisor:
# it starts a real /kaniko/executor build whose RUN step sleeps, then launches
# the helper as its own child, as a Concourse intercept does. Staging uses the
# documented script, stdin stream and expected digest.
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

container=$(docker run -d --platform linux/amd64 --entrypoint /busybox/sh \
  -v "$work/context:/workspace/context:ro" -w /workspace "$image" -c '
    # Kaniko wipes everything except /kaniko and mounts before unpacking the
    # base image, so keep all smoke state under /kaniko.
    /busybox/mkdir -p /kaniko/smoke
    /kaniko/executor --dockerfile=context/Dockerfile --context=context \
      --use-new-run --single-snapshot --cache=false --no-push \
      --tar-path=/kaniko/smoke/image.tar --digest-file=/kaniko/smoke/digest > /kaniko/smoke/kaniko.log 2>&1 &
    while [ ! -e /kaniko/smoke/go ]; do /busybox/sleep 1; done
    /busybox/sleep 2  # let the exec that wrote the marker exit first
    set +e
    '"$runtime"' -I -c "$(/busybox/cat /kaniko/smoke/helper.py)" --apply $(/busybox/cat /kaniko/smoke/args) 2>&1
    echo "SMOKE-RC=$?"
    exec /busybox/sleep 600')

in_container() { docker exec -i "$container" "$@"; }

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
  docker exec "$container" /busybox/cat /kaniko/smoke/kaniko.log >&2 || true
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
        start = raw[raw.rfind(")") + 2:].split()[19]
        print(entry.name, start, hashlib.sha256(command).hexdigest(), os.readlink(entry / "cwd"))
')
read -r pid _ _ _ <<< "$args"
[ -n "${pid:-}" ] || { docker exec "$container" /busybox/cat /kaniko/smoke/kaniko.log >&2; echo "executor not found" >&2; exit 1; }
in_container /busybox/sh -c 'test -e /proc/'"$pid"
echo "Executor identity: $args"

printf '%s\n' "$helper" | in_container /busybox/sh -c '/busybox/cat > /kaniko/smoke/helper.py'
printf '%s\n' "$args" | in_container /busybox/sh -c '/busybox/cat > /kaniko/smoke/args && /busybox/touch /kaniko/smoke/go'

# Never exec into the container while the helper runs: it would be an
# unrelated live process and make the helper fail closed.
for _ in $(seq 1 90); do
  if docker logs "$container" 2>&1 | grep -q '^SMOKE-RC='; then break; fi
  sleep 1
done
result=$(docker logs "$container" 2>&1)
echo "$result"
python3 -c '
import json, sys
lines = sys.argv[1].strip().splitlines()
assert "SMOKE-RC=0" in lines, "helper failed or did not finish"
receipt = json.loads(lines[lines.index("SMOKE-RC=0") - 1])
assert receipt["stopped_processes"] >= 2, receipt
print("receipt", receipt)
' "$result"

echo "Verify: no live process remains outside the supervisor"
in_container "$runtime" -I -c '
import os, pathlib, sys
live = []
for entry in pathlib.Path("/proc").iterdir():
    if entry.name.isdigit() and int(entry.name) not in (1, os.getpid()):
        raw = (entry / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] != "Z" and int(fields[1]) != 0:
            live.append((entry.name, (entry / "cmdline").read_bytes()))
sys.exit(f"live processes remain: {live}" if live else 0)
'
echo "Orphan cleanup smoke test passed"
