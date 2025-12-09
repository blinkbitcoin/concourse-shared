# Architecture

## Overview

concourse-shared is a CI/CD infrastructure repository that distributes shared CI tasks and GitHub Actions across multiple repositories in the blinkbitcoin organization.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    concourse-shared repo                         │
├─────────────────────────────────────────────────────────────────┤
│  shared/actions/     shared/ci/tasks/      images/              │
│  (GH Actions)        (Concourse tasks)     (Dockerfiles)        │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Concourse CI Pipeline                         │
├─────────────────────────────────────────────────────────────────┤
│  bump-shared-files-in-*  │  build-*-image  │  backup-org-to-gcp │
└──────────────┬───────────┴────────┬────────┴────────────────────┘
               │                    │
               ▼                    ▼
┌──────────────────────┐  ┌─────────────────────────────┐
│   Target Repos (7)   │  │   us.gcr.io/galoy-org       │
│   via vendir sync    │  │   Docker images (4)         │
└──────────────────────┘  └─────────────────────────────┘
```

## Components

### Pipeline Definition (`ci/pipeline.yml`)

ytt-templated Concourse pipeline with three job groups:

1. **bump-shared-files** - For each target repo:
   - Clones concourse-shared
   - Runs `bump-shared-files.sh` with repo's feature flags
   - Uses vendir to sync appropriate files
   - Creates PR in target repo

2. **images** - Builds CI runner images:
   - Uses Kaniko for in-cluster builds
   - Pushes to Google Container Registry

3. **backups** - Daily org backup:
   - Clones all blinkbitcoin repos
   - Compresses and uploads to GCS

### Shared Tasks (`shared/ci/tasks/`)

Reusable shell scripts for CI operations:

| Script | Purpose |
|--------|---------|
| `nodejs-helpers.sh` | `unpack_deps()`, `check_code()` functions |
| `nodejs-check-code.sh` | Runs pnpm/yarn code:check |
| `nodejs-audit.sh` | Security audit with configurable level |
| `rust-helpers.sh` | Cargo environment setup |
| `rust-check-code.sh` | Runs `make check-code` in nix develop |
| `prep-release-src.sh` | Generates changelog, bumps version |
| `docker-prep-docker-build-env.sh` | Sets VERSION, COMMITHASH, BUILDTIME |
| `chart-open-charts-pr.sh` | Creates PR to bump image in charts repo |

### Pipeline Fragments (`shared/ci/pipeline-fragments.lib.yml`)

ytt library providing reusable pipeline components:

- Task image configs (`nodejs_task_image_config()`, `rust_task_image_config()`)
- Job definitions (`nodejs_check_code()`, `rust_check_code()`, `build_edge_image()`)
- Resource definitions (`repo_resource()`, `edge_image_resource()`)
- Resource types (`gcs-resource`, `slack-notification`)

### Docker Images (`images/`)

| Image | Base | Key Tools |
|-------|------|-----------|
| nodejs-concourse | node:20-bookworm | pnpm, docker, gcloud, gh-cli, bats |
| rust-concourse | rust:latest | cargo-nextest, clippy, sqlx-cli, docker |
| release-pipeline | python:3.8-buster | git-cliff, helm, kubectl, bump2version |
| wincross | rust:latest | mingw-w64, x86_64-pc-windows-gnu target |

## Configuration

### `ci/values.yml`

Defines target repositories and their features:

```yaml
src_repos:
  repo-name: ["nodejs"]           # Node.js only
  repo-name: ["rust", "docker"]   # Rust with Docker
  repo-name: ["nodejs", "docker", "chart"]  # Full stack
```

### `vendir.tmpl.yml`

Controls file distribution via excludePaths:
- Feature-prefixed files excluded by default
- `bump-shared-files.sh` removes excludes for enabled features

## Security

- GitHub App authentication (`gh-token` for JWT generation)
- GCP service account for GCS access
- SSH keys for remote host execution
- Docker registry credentials via Concourse secrets
