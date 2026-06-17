# Development Guide

## Prerequisites

- Nix with flakes enabled
- direnv (optional, for automatic environment loading)
- Access to blinkbitcoin GitHub org

## Environment Setup

```bash
# Clone the repository
gh repo clone blinkbitcoin/concourse-shared
cd concourse-shared

# Enter dev environment (provides ytt, alejandra)
nix develop

# Or with direnv (automatic)
direnv allow
```

## Development Workflow

### Adding a New Target Repository

1. Edit `ci/values.yml`:
   ```yaml
   src_repos:
     new-repo-name: ["nodejs"]  # or ["rust"], ["nodejs", "docker", "chart"], etc.
   ```

2. Push changes and run pipeline:
   ```bash
   ci/repipe
   ```

3. Ensure `galoybot` has repo permissions and `blinkbitcoinbot` label exists

### Feature Flags

| Flag | Description |
|------|-------------|
| `nodejs` | Syncs Node.js tasks (check-code, audit, helpers) |
| `rust` | Syncs Rust tasks (check-code, helpers) |
| `docker` | Syncs Docker build tasks |
| `chart` | Syncs Helm chart tasks |

Files without a feature prefix are synced to all repos.

### Modifying Shared Tasks

1. Edit files in `shared/ci/tasks/` or `shared/actions/`
2. Commit and push to main
3. Pipeline auto-triggers and creates PRs in all target repos

### Building Docker Images

Images auto-build when their Dockerfile changes:
- `images/nodejs-concourse/Dockerfile` → `us.gcr.io/galoy-org/nodejs-concourse`
- `images/rust-concourse/Dockerfile` → `us.gcr.io/galoy-org/rust-concourse`
- `images/release/Dockerfile` → `us.gcr.io/galoy-org/release-pipeline`
- `images/wincross/Dockerfile` → `us.gcr.io/galoy-org/wincross-rust`

## Pipeline Structure

### Pipeline Groups

| Group | Jobs |
|-------|------|
| `bump-shared-files` | One job per target repo |
| `images` | 4 image build jobs |
| `backups` | Daily GitHub org backup |

### Vendir Sync Process

The `vendir.tmpl.yml` template controls file syncing:
- `shared/actions/*` → `.github/workflows/vendor/`
- `shared/ci/**/*` → `ci/vendor/`

Feature-specific files are excluded via `excludePaths` and selectively included based on repo's feature flags.

## Testing Changes

No local test suite. Changes are validated by:
1. Pipeline execution success
2. Target repo CI passing after PR merge
