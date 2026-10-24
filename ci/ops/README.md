# Stop an orphaned image build

Task: https://github.com/blinkbitcoin/blink-wip/issues/1808

Use this only after an operator authorizes cleanup of a terminal Concourse build
whose Kaniko executor is still running. It does not stop a worker, remove a
container, modify image contents, publish an image, or restart a build.

## Preconditions

1. Pause the affected image job. Read `fly -t TARGET builds --job PIPELINE/JOB
   --json` and confirm the intended build is terminal with no newer or active
   build. Inspect its resources and retained log: no publication may be in doubt.
2. Read `fly -t TARGET containers --json`. Select exactly one record matching
   team, build ID, pipeline, job, `step_name=build`, and `type=task`. Record its
   `id` and `worker_name`. Use that exact container ID with `--handle` below.
3. In that **terminal build container only**, inspect `/proc/PID/stat`,
   `/proc/PID/cmdline` and `/proc/PID/cwd`. Record the Kaniko PID (never 1), its
   start time (stat field 22), SHA256 of the exact NUL-separated cmdline bytes,
   and working directory. Confirm `/kaniko/executor` and the expected source
   context, Dockerfile, no-push, archive and digest arguments. Record process
   ancestry and refuse unrelated or ambiguous workloads.
4. Recheck the external build/container identity immediately before cleanup.
   Keep other candidate jobs paused. Stop if the container or source changed.

## Cleanup

Use the reviewed helper's exact source bytes as Python's `-c` argument (or a
verified identical staged copy). Do not interpolate process data into code.
For example, from a trusted local Python operator session:

```python
source = Path("ci/ops/stop-orphaned-image-build.py").read_text()
subprocess.run([
    "fly", "-t", target, "intercept", "--handle", container_id, "--",
    "/usr/bin/python3", "-c", source, "--apply", str(pid), str(start),
    command_sha256, working_directory,
], check=True)
```

The helper first verifies that the retained task container has no unrelated live
workload, then pins and freezes the root with a Linux pidfd before its final
command and working-directory check. It protects only the helper/supervisor
ancestry and freezes every other live process in the dedicated container, so a
descendant cannot leave scope merely by reparenting. It refuses self/init targets,
PID reuse or more than 4096 processes. It sends TERM and CONT only through pinned
descriptors, waits 10 seconds, and sends KILL to survivors. It trusts only PID 1
as the shared supervisor boundary and rejects a target sharing any other ancestor
with the helper. It then reconciles the complete container process inventory until
two scans are clean. Any late process makes cleanup fail closed; the helper does
not signal it because it cannot distinguish a workload fork from a concurrent
administrative exec. On preflight failure it attempts to resume every process it
stopped; after TERM delivery starts or becomes uncertain it never resumes a
partially terminated workload. It requires Linux and Python with pidfd support.
Do not weaken a failed guard.

Termination is irreversible; recovery is a new build from reviewed source, never
resumption of a partially terminated compiler tree. Retain the helper commit,
identity evidence and receipt in protected operator storage.

## Verification and retry

Independently enumerate the complete `/proc` process inventory in the **same
terminal container** and require that no live process remains outside the known
container supervisor ancestry. Do not rely on command names or descendants of the
original Kaniko PID: a late fork can change both its name and parent. Zombies are
exited processes, not active compilation. Recheck that no publication occurred
and that no newer build is active. Only then unpause and trigger at most one clean
retry; first check whether normal scheduling already created it.

Monitor the retry using `fly watch`, build status and resource APIs only. Do not
intercept a running build: Concourse 8.3.0 stores a container-wide exit status,
which can obscure a missing task's result during reattachment. This is a risk,
not proof of the cause of any particular reinitialization. Missing archive or
digest evidence must continue to block publication. Stop again on uncertainty.

## Local validation

```sh
python3 -m unittest discover -s tests -p 'test_orphan_cleanup.py'
python3 -m coverage run --branch --source=ci/ops -m unittest discover -s tests -p 'test_orphan_cleanup.py'
python3 -m coverage report --fail-under=100
```

The unit tests simulate Linux process primitives; they do not prove a production
cleanup. Record actual process exit and independent post-cleanup evidence too.
