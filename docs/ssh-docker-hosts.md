# SSH Docker hosts

Shared fragments and tasks for running Concourse jobs on Docker hosts that are
reached over plain SSH on a private address. The hosts are grouped into sites:
`site1`, `site2` and so on. Each site has its own pool, `docker-hosts-<site>`,
in `blinkbitcoin/concourse-locks`. The site names are deliberately opaque;
which site is where is recorded with the host provisioning, in the private
infrastructure repository.

The site pools replace the GCP `docker-hosts` pool
(`galoy-staging/docker-host-0` and `docker-host-1`), which is reached with
`gcloud compute ssh --tunnel-through-iap` and cannot use the site hosts.

The transport was first built for blink core/apps in
blinkbitcoin/blink-legacy#926, which was closed unmerged in favour of this
copy; it is maintained here so every consumer can vendor it.

## Fragments

The fragments keep their names and take an optional `site`:

| Without `site` (GCP, renders as before) | With `site` |
| --- | --- |
| `docker_host_pool()` | `docker_host_pool(site="site1")` |
| `run_on_nix_host(name, cmd, additional_params={})` | `run_on_nix_host(name, cmd, additional_params={}, site="site1", workload_timeout_minutes=12)` |
| `test_on_docker_host(container, additional_params={})` | `test_on_docker_host(container, additional_params={}, site="site1", workload_timeout_minutes=20)` |

`site` must look like `site1`; anything else fails the render.
`workload_timeout_minutes` applies only with `site` and fails the render
without it.

A site's pool resource is named `docker-host-<site>`, so a pipeline can use the
GCP pool and one or more sites at once while it migrates job by job. The tasks
always read the claim from an input named `docker-host`; the fragment maps the
site's resource to it. The GCP path stays available until every consumer has
moved and is removed with the GCP hosts; `site` becomes required then.

Adding a site needs no fragment change: admit its hosts to a new
`docker-hosts-<site>` pool and pass that site name.

What a job with `site` does:

- Fetches `repo` and `pipeline-tasks` first, then claims one host and runs
  once (`attempts: 1`) on an untagged Linux worker. Fetching first means a
  failed `get` cannot leave a claim behind.
  Only workers in the hosts' private network can reach their addresses, so
  the `galoy-staging` tag is gone.
- Verifies the host metadata (private RFC1918 address, pinned Ed25519 host
  key, hostname) and refuses before any host access if the task image lacks
  `jq`, `openssl`, `curl`, `ssh`, `rsync`, `timeout` or `envsubst`.
- Prepares the repository on the worker as the GCP tasks did: `.env.ci` is
  rendered to `tmp.env.ci` with `envsubst` (nix mode), or
  `make create-tmp-env-ci` runs (compose mode). The SSH key and service-account
  JSON are unset first, so they are neither inherited nor substituted into
  these files. Repository code is trusted as on the GCP path: running as the
  same user, it can still read the task's initial environment from `/proc`,
  and a process it leaves running could write the cleanup receipt.
- Removes all containers and the previous copy of the repository on the host
  (with `sudo`, because Docker bind mounts leave root-owned files), then copies
  the repository with `rsync`.
- Logs Docker in to `us.gcr.io` with a short-lived token scoped to
  `devstorage.read_only`. The service-account JSON never leaves the worker.
- Sends `additional_params` (listed in `PARAMS_KEYS`) to the host as a private
  file in the per-run directory, over SSH stdin. Values never appear in command
  lines or logs, and the file is deleted by cleanup. Values the repository
  renders itself (for example into `tmp.env.ci`) stay in the repository copy
  on the host until that job's next run removes it, as on the GCP hosts.
  `additional_params` keys must be shell variable names. Names the transport
  or the fragment sets itself (`PATH`, `HOME`, `CMD`, `PACKAGE_DIR`,
  `DOCKER_CONFIG`, `LD_*`, `PARAMS_KEYS`, `WORKLOAD_TIMEOUT_MINUTES`, the
  credential params, `JEST_TIMEOUT` in compose mode and others) fail the
  render instead of being silently replaced.
- Runs the workload under a remote supervisor with a hard limit of
  `workload_timeout_minutes`:
  - nix mode: `nix develop -c bash -c "$CMD"` in the repository;
  - compose mode: `docker compose pull`, `up <container> --attach <container>`,
    the test container's exit status (found by its compose project and
    service labels), then `docker compose down`, which also runs after a
    workload timeout. A cancelled build does not run it; the next claim
    removes all containers before it starts.
- Cleans up the remote process group and credentials, then writes a cleanup
  receipt. The task timeout is the workload limit plus 25 minutes, more than
  the sum of every bounded transport step, so a workload timeout still leaves
  time for cleanup.

## Release gate and quarantine

The job's `ensure` step runs `ssh-host-check-cleanup.sh` before releasing the
host. It releases only when the cleanup receipt matches the claimed metadata.
A workload failure with confirmed cleanup releases the host normally.

A failure before the first SSH command (invalid job input, a reserved or
malformed `PARAMS_KEYS` entry, a broken service-account key) never touched the
host, so the task writes the receipt and the host is released. Invalid claim
metadata or a task image without the required clients keeps the claim.

Failed cleanup, cancellation without a receipt, or a failure after the first
SSH command but before the run directory exists keeps the claim. That host is
quarantined: no other job can take it. Before releasing it:

1. Identify the exact host and build from the failed build log.
2. On that host, confirm no workload from that run is still running
   (`.ci-run/run.*/runner.pid`, `ps -eo pid,pgid,stat,args`), and stop it if so.
3. Delete the run's `docker` directory and `env` file if present.
4. Keep the failed build and the run directory as evidence.
5. Release that exact claim. No job in this repository does it, so make the
   same change the pool resource's `release` makes: in
   `blinkbitcoin/concourse-locks`, one commit that runs
   `git mv docker-hosts-<site>/claimed/<host> docker-hosts-<site>/unclaimed/<host>`
   and names the failed build. Do not touch any other entry, release a claim
   you have not inspected, or move an entry into another pool.

## Migrating a consumer

One pull request per consumer repository, so each can be reverted on its own.

1. Vendor a `concourse-shared` ref that contains these fragments: bump
   `ci/vendir.yml` and run `vendir sync` (or merge the propagated
   `bump-shared-tasks` PR).
2. In `ci/pipeline.yml`, add `site` to the fragment calls and add the site's
   pool resource:

   ```yaml
   #@ load("vendor/pipeline-fragments.lib.yml", "docker_host_pool", "run_on_nix_host")
   jobs:
   - #@ run_on_nix_host("integration-tests", "make test-in-ci", {"RAIN_API_KEY": data.values.rain_api_key}, site="site1")
   resources:
   - #@ docker_host_pool(site="site1")
   ```

   Repositories with their own copy of `run_on_nix_host` (stablesats-rs,
   blink-fiat, blink-kyc, blink-routing) delete it and use the shared fragment.
   Pass any variable their copy exported on the host as `additional_params`
   (blink-routing exports `DOCKER_API_VERSION`; check that the site's Docker
   daemon still accepts that API version before keeping it).
   Set `workload_timeout_minutes` if a job needs more than the default.
3. Render with `ytt -f ci` and compare against the live pipeline
   (`fly get-pipeline`). Only the migrated jobs and the new resource should
   change.
4. Repipe, run each migrated job once, and confirm the host was released
   (`docker-hosts-<site>/unclaimed` in `concourse-locks`).
5. When no job uses it, remove the `docker_host_pool()` call without `site`
   from the pipeline.

Rollback: revert the consumer pull request and repipe. Check for running
builds and held claims first; a held site claim must be released through the
procedure above, not by switching pools.

## Credentials

Unchanged Concourse secrets: `staging-ssh.ssh_private_key` (the `ci` user's
key on the hosts; the public key is not needed) and
`staging-gcp-creds.creds_json` (only used to mint the read-only registry
token).

## Not covered here

- Host provisioning and pool admission. The `site1` hosts were qualified and
  admitted by a qualification pipeline whose source is in
  blinkbitcoin/blink-legacy at `d985b9a` (`ci/shared/`). Port it here before
  adding hosts or a site.
- blink core/apps do not use these fragments yet. Their live pipelines run the
  #926 copy of this transport from blink-legacy; moving them here is follow-up
  work.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_ssh_host*.py' -v
shellcheck shared/ci/tasks/ssh-host-*.sh
```

Process-group and remote-supervisor tests need Linux; they skip on macOS. The
fragment tests need `ytt`. The `SSH Docker-host transport` workflow runs
both on GitHub Actions.
