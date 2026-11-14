#!/usr/bin/env bash
# Executed as ci over the already authenticated, pinned SSH connection.
# Arguments: run directory, repository path, package directory, mode
# (nix|compose), workload timeout in minutes, then the command (nix) or the
# compose test service (compose).
set -euo pipefail
run_dir="$HOME/$1"
mode=$4
minutes=$5
[[ $mode == nix || $mode == compose ]] || { echo 'unsupported workload mode' >&2; exit 1; }
[[ $minutes =~ ^[1-9][0-9]{0,2}$ ]] || { echo 'invalid workload timeout' >&2; exit 1; }
export CI_RUN_DIR="$run_dir" REPO_PATH="$HOME/$2" PACKAGE_DIR="$3"
export CI_MODE="$mode" CI_TIMEOUT="${minutes}m"
export DOCKER_CONFIG="$run_dir/docker"
if [[ $mode == nix ]]; then
  export CMD="$6"
else
  export TEST_CONTAINER="$6"
fi
# Same steps as the GCP test-on-docker-host.sh, but the test container's exit
# status is always followed by compose down. No nounset: .envrc may use unset
# variables or direnv functions, as on the GCP hosts.
cat > "$run_dir/compose-test.sh" <<'COMPOSE'
#!/usr/bin/env bash
project=$(basename "$REPO_PATH")
export TMP_ENV_CI=tmp.env.ci COMPOSE_PROJECT_NAME="$project"
# shellcheck disable=SC1091
source .envrc > /dev/null 2>&1 || true
docker compose pull
docker compose -f docker-compose.yml up "$TEST_CONTAINER" --attach "$TEST_CONTAINER"
up=$?
status=1
ids=$(docker ps -aq -f status=exited -f "name=${project}-${TEST_CONTAINER}-")
if [[ $up != 0 ]]; then
  status=$up
elif [[ $ids =~ ^[0-9a-f]+$ ]]; then
  status=$(docker inspect "$ids" --format '{{.State.ExitCode}}') || status=1
  [[ $status =~ ^[0-9]+$ ]] || status=1
else
  echo "Could not identify the exited $TEST_CONTAINER container" >&2
fi
docker compose down --remove-orphans --timeout 1
exit "$status"
COMPOSE
# Also serves as the launch-attempt marker for fallback cleanup. Keep this
# last before setsid so interruption before runner.pid publication fails closed.
cat > "$run_dir/command.sh" <<'COMMAND'
#!/usr/bin/env bash
set +e
workload() {
  if test -f "$CI_RUN_DIR/env"; then
    # shellcheck disable=SC1091
    . "$CI_RUN_DIR/env" || return 1
  fi
  case $CI_MODE in
    nix)
      cd "$REPO_PATH/$PACKAGE_DIR" &&
        timeout --foreground --kill-after=15s "$CI_TIMEOUT" nix develop -c bash -c "$CMD"
      ;;
    compose)
      cd "$REPO_PATH" &&
        timeout --foreground --kill-after=15s "$CI_TIMEOUT" bash "$CI_RUN_DIR/compose-test.sh"
      ;;
    *) return 1 ;;
  esac
}
workload
printf '%s\n' "$?" > "$CI_RUN_DIR/status"
# Keep the session leader alive until the supervising shell terminates the
# whole group, including any children left behind by the workload.
while :; do sleep 60; done
COMMAND
setsid bash "$run_dir/command.sh" > "$run_dir/log" 2>&1 < /dev/null &
runner=$!
printf '%s\n' "$runner" > "$run_dir/runner.pid"
# shellcheck disable=SC2317,SC2329 # Invoked by the EXIT trap.
cleanup() {
  kill -TERM -- "-$runner" 2>/dev/null || true
  sleep 1
  kill -KILL -- "-$runner" 2>/dev/null || true
  wait "$runner" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
tail -n +1 -f --pid="$runner" "$run_dir/log" &
while ! test -f "$run_dir/status"; do
  kill -0 "$runner"
  sleep 1
done
exit "$(cat "$run_dir/status")"
