# ZRH1 SSH Docker hosts

Shared fragments and tasks for running Concourse jobs on the ZRH1 Docker-host
pool (`zrh1-docker-hosts` in `blinkbitcoin/concourse-locks`). They replace the
GCP `docker-hosts` pool (`galoy-staging/docker-host-0` and `docker-host-1`),
which is reached with `gcloud compute ssh --tunnel-through-iap` and cannot use
the ZRH1 hosts.

The transport was first built for blink core/apps
(blinkbitcoin/blink-legacy#926) and is maintained here so every consumer can
vendor it. The four hosts are provisioned in `zrh1-infra`
(`docs/operations/docker-host-vms.md`).

## Fragments

| GCP fragment (unchanged) | ZRH1 replacement |
| --- | --- |
| `docker_host_pool()` | `zrh1_docker_host_pool()` |
| `run_on_nix_host(name, cmd, additional_params={})` | `run_on_zrh1_nix_host(name, cmd, additional_params={}, workload_timeout_minutes=12)` |
| `test_on_docker_host(container, additional_params={})` | `test_on_zrh1_docker_host(container, additional_params={}, workload_timeout_minutes=20)` |

The ZRH1 pool resource is named `zrh1-docker-host`, so a pipeline can run both
pools while it migrates job by job. Both fragment sets stay available until
every consumer has moved; the GCP set is removed with the GCP hosts.

What the job does:

- Claims one host, then runs once (`attempts: 1`) on an untagged Linux worker.
  Only workers in the ZRH1 VM cluster can reach the hosts' private Service
  addresses, so the `galoy-staging` tag is gone.
- Verifies the host metadata (private RFC1918 address, pinned Ed25519 host
  key, hostname) and refuses before any host access if the task image lacks
  `jq`, `openssl`, `curl`, `ssh`, `rsync`, `timeout` or `envsubst`.
- Prepares the repository on the worker as the GCP tasks did: `.env.ci` is
  rendered to `tmp.env.ci` with `envsubst` (nix mode), or
  `make create-tmp-env-ci` runs (compose mode). The SSH key and service-account
  JSON are unset first, so they can never be substituted into these files.
- Removes all containers and the previous copy of the repository on the host
  (with `sudo`, because Docker bind mounts leave root-owned files), then copies
  the repository with `rsync`.
- Logs Docker in to `us.gcr.io` with a short-lived token scoped to
  `devstorage.read_only`. The service-account JSON never leaves the worker.
- Sends `additional_params` (listed in `PARAMS_KEYS`) to the host as a private
  file in the per-run directory, over SSH stdin. Values never appear in command
  lines or logs, and the file is deleted by cleanup.
- Runs the workload under a remote supervisor with a hard limit of
  `workload_timeout_minutes`:
  - nix mode: `nix develop -c bash -c "$CMD"` in the repository;
  - compose mode: `docker compose pull`, `up <container> --attach <container>`,
    the test container's exit status, then `docker compose down`.
- Cleans up the remote process group and credentials, then writes a cleanup
  receipt. The task timeout is the workload limit plus 10 minutes, so a
  workload timeout still leaves time for cleanup.

## Release gate and quarantine

The job's `ensure` step runs `ssh-host-check-cleanup.sh` before releasing the
host. It releases only when the cleanup receipt matches the claimed metadata.
A workload failure with confirmed cleanup releases the host normally.

Failed cleanup, cancellation without a receipt, or a failure before the run
directory exists keeps the claim. That host is quarantined: no other job can
take it. Before releasing it:

1. Identify the exact host and build from the failed build log.
2. On that host, confirm no workload from that run is still running
   (`.ci-run/run.*/runner.pid`, `ps -eo pid,pgid,stat,args`), and stop it if so.
3. Delete the run's `docker` directory and `env` file if present.
4. Keep the failed build and the run directory as evidence.
5. Release that exact claim through the pool resource. Do not edit the locks
   repository by hand, release a claim you have not inspected, or move an
   entry into the GCP pool.

## Migrating a consumer

One pull request per consumer repository, so each can be reverted on its own.

1. Vendor a `concourse-shared` ref that contains these fragments: bump
   `ci/vendir.yml` and run `vendir sync` (or merge the propagated
   `bump-shared-tasks` PR).
2. In `ci/pipeline.yml`, replace the fragment calls and the pool resource:

   ```yaml
   #@ load("vendor/pipeline-fragments.lib.yml", "zrh1_docker_host_pool", "run_on_zrh1_nix_host")
   jobs:
   - #@ run_on_zrh1_nix_host("integration-tests", "make test-in-ci", {"RAIN_API_KEY": data.values.rain_api_key})
   resources:
   - #@ zrh1_docker_host_pool()
   ```

   Repositories with their own copy of `run_on_nix_host` (stablesats-rs,
   blink-fiat, blink-kyc) delete it and use the shared fragment.
   Set `workload_timeout_minutes` if a job needs more than the default.
3. Render with `ytt -f ci` and compare against the live pipeline
   (`fly get-pipeline`). Only the migrated jobs and the new resource should
   change.
4. Repipe, run each migrated job once, and confirm the host was released
   (`zrh1-docker-hosts/unclaimed` in `concourse-locks`).
5. When no job uses it, remove `docker_host_pool()` from the pipeline.

Rollback: revert the consumer pull request and repipe. Check for running
builds and held claims first; a held ZRH1 claim must be released through the
procedure above, not by switching pools.

## Credentials

Unchanged Concourse secrets: `staging-ssh.ssh_private_key` (the `ci` user's
key on the hosts; the public key is not needed) and
`staging-gcp-creds.creds_json` (only used to mint the read-only registry
token).

## Not covered here

- Host provisioning and pool admission. The four hosts were qualified and
  admitted by the `zrh1-docker-host-qualification` pipeline, whose source is
  on the `ci/zrh1-docker-transport` branch of blinkbitcoin/blink-legacy at
  `d985b9a` (`ci/shared/`). Port it here before adding hosts.
- blink core/apps still use their own copy of this transport.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_ssh_host*.py' -v
shellcheck shared/ci/tasks/ssh-host-*.sh
```

Process-group and remote-supervisor tests need Linux; they skip on macOS. The
fragment tests need `ytt`. The `ZRH1 SSH Docker-host transport` workflow runs
both on GitHub Actions.
