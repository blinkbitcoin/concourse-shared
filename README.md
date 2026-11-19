# concourse-shared
This repo contains:
* shared CI tasks [synced](https://ci.blink.sv/teams/dev/pipelines/blink-concourse-shared?group=bump-shared-files) across repositories. 
* It also contains the [CI pipeline](https://ci.blink.sv/teams/dev/pipelines/blink-concourse-shared?group=images) for image creation needed by CI
* and a backup [pipeline](https://ci.blink.sv/teams/dev/pipelines/blink-concourse-shared?group=backups) to backup all repositories of the blinkbitcoin organization and push them to the [backup bucket](https://console.cloud.google.com/storage/browser/blink-backups).

### Steps for adding in a new repository:

1. Clone and get into the repository:

```
gh repo clone blinkbitcoin/concourse-shared
cd concourse-shared
```

2. Edit `ci/values.yml` and under `src_repos`, add the new repository. The key name for the repository must be the name of the repository as on GitHub under blinkbitcoin organization.
   This file contains many feature flags according to which the Pull Request will be created with shared tasks.
3. Push the change on the CI.

```
ci/repipe
```

4. Make sure that `galoybot` has permissions to the target repository and it also has `galoybot` as a possible label in the PR.

This would, in turn create a new job under the concourse-shared pipeline and when it runs, it would automatically create the pull request for you on the specified target repository.

### Shared Folder Details (shared/\*\*)

1. `actions` folder - Gets synced to `.github/workflows/vendor` folder
2. `ci/tasks` folder - Get synced to `ci/vendor` folder

### Feature Flags

Feature Flags `nodejs`, `rust`, `chart` and `docker` are supported right now.
Files whose names don't start with them are treated as common and synced to all.

| Feature | Description                                              |
| ------- | -------------------------------------------------------- |
| Nodejs  | Source Codebase is Node.js                               |
| Rust    | Source Codebase is Rust                                  |
| Docker  | Docker image is present in the source                    |
| Chart   | The docker image getting generated also has a Helm Chart |

#### nodejs

- GH Actions:
  - Check Code (`pnpm code:check` after `pnpm install`, pnpm only)
  - Audit (`node ci/vendor/tasks/nodejs-audit-advisories.js --level high`, see below)

- Concourse CI:
  - supports yarn and pnpm
  - Helpers (`unpack_deps` for caching node_modules)
  - Install Deps (autodetects pckMgr and then `yarn install` or `pnpm install`)
  - Check Code (autodetects pckMgr and then  `yarn code:check` or `pnpm code:check`)
  - Audit (`nodejs-audit-advisories.js --level ${LEVEL}`, default `high`)

`nodejs-audit-advisories.js` replaces `yarn audit` and `pnpm audit`, whose legacy registry endpoints npmjs.org retired on 2026-07-15. It reads the production dependency graph (`dependencies` and `optionalDependencies` of the project and its workspaces) from `yarn.lock` (v1) or `pnpm-lock.yaml` (lockfileVersion 6 or 9), with `yarn.lock` taking precedence, and posts it to the npm bulk advisory endpoint. When the graph cannot be resolved completely it audits every package in the lockfile instead. It exits 1 on advisories at or above the level and 2 when the audit cannot run (unsupported lockfile, registry error or unreadable response), so a skipped audit never passes.

#### rust

- GH Actions:
  - Check Code (`make check-code`)

- Concourse CI:
  - Helpers (Some `CARGO_` envs)
  - Check Code (`make check-code`)

#### docker

- Concorse CI:
  - Prep Docker Build Env

#### chart

- Concourse CI:
  - Open Charts PR

### Docker hosts

Jobs that need a Docker host pass a site to the Docker-host fragments, for
example `run_on_nix_host(..., site="site1")`, `test_on_docker_host(...,
site="site1")` and `docker_host_pool(site="site1")`. Without `site` the
fragments still render the GCP pool, until every consumer has migrated. See
[docs/ssh-docker-hosts.md](docs/ssh-docker-hosts.md).
