# Stage the pinned, fully static CPython used by stop-orphaned-image-build.py inside a
# retained Kaniko task container. The Kaniko debug image has no Python, so the
# runtime (built by build-cleanup-runtime.py) is streamed in on stdin and
# verified with the image's own BusyBox.
#
# Run with the image's BusyBox, passing these exact bytes as the -c argument:
#   /busybox/sh -c "$SCRIPT" stage RUNTIME_SHA256 < runtime.tar
# See ci/ops/README.md. /kaniko is excluded from Kaniko snapshots, so staging
# there cannot leak into image layers.
set -eu
# Use only the image's own BusyBox, never tools from the unpacked build rootfs.
# shellcheck disable=SC2123
PATH=/busybox
expected=${1:-}
case $expected in
  *[!0-9a-f]* | '') echo "expected SHA256 must be lowercase hex" >&2; exit 2 ;;
esac
if [ ${#expected} -ne 64 ]; then
  echo "expected SHA256 must be 64 hex characters" >&2
  exit 2
fi
dir=/kaniko/orphan-cleanup-runtime
if [ -e "$dir" ]; then
  echo "$dir already exists; inspect or remove it explicitly before restaging" >&2
  exit 1
fi
umask 077
mkdir "$dir"
trap 'rm -rf "$dir"' EXIT
cat > "$dir/runtime.tar"
echo "$expected  $dir/runtime.tar" | sha256sum -c - > /dev/null
tar -xf "$dir/runtime.tar" -C "$dir"
rm "$dir/runtime.tar"
"$dir/python/bin/python3" -I -c '
import os, signal, sys
assert sys.version_info >= (3, 11), sys.version
assert hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"), "no pidfd support"
print("staged Python", sys.version.split()[0])
'
trap - EXIT
