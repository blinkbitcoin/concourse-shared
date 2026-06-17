# Source Tree Analysis

## Directory Structure

```
concourse-shared/
├── ci/                          # Concourse CI pipeline definitions
│   ├── pipeline.yml             # Main pipeline (ytt template)
│   ├── values.yml               # Pipeline configuration values
│   ├── build/
│   │   └── pipeline.yml         # Generated/compiled pipeline
│   └── tasks/                   # Pipeline-specific tasks
│       ├── bump-shared-files.sh # Syncs shared files to target repos
│       ├── open-pr.sh           # Creates PRs in target repos
│       └── gcp-backup.sh        # GitHub org backup to GCS
│
├── shared/                      # Shared CI resources (synced to repos)
│   ├── actions/                 # GitHub Actions workflows
│   │   ├── nodejs-check-code.yml
│   │   ├── nodejs-audit.yml
│   │   ├── rust-check-code.yml
│   │   ├── rust-audit.yml
│   │   └── spelling.yml
│   └── ci/
│       ├── pipeline-fragments.lib.yml  # Reusable ytt pipeline functions
│       ├── config/
│       │   ├── git-cliff.toml          # Changelog generation config
│       │   ├── nodejs-dependabot.yml
│       │   └── rust-dependabot.yml
│       └── tasks/                      # Shared CI scripts
│           ├── nodejs-helpers.sh       # Node.js utility functions
│           ├── nodejs-check-code.sh
│           ├── nodejs-audit.sh
│           ├── nodejs-cache-yarn-deps.sh
│           ├── nodejs-cache-pnpm-deps.sh
│           ├── nodejs-update-package-json.sh
│           ├── rust-helpers.sh         # Rust utility functions
│           ├── rust-check-code.sh
│           ├── docker-prep-docker-build-env.sh
│           ├── docker-bump-image-digest.sh
│           ├── chart-open-charts-pr.sh
│           ├── chart-test-integration.sh
│           ├── prep-release-src.sh     # Release automation
│           ├── run-on-nix-host.sh      # Remote Nix execution
│           └── test-on-docker-host.sh  # Remote Docker testing
│
├── images/                      # Docker image definitions
│   ├── nodejs-concourse/
│   │   └── Dockerfile           # Node.js 20 CI image
│   ├── rust-concourse/
│   │   └── Dockerfile           # Rust CI image
│   ├── release/
│   │   └── Dockerfile           # Release pipeline image
│   └── wincross/
│       └── Dockerfile           # Windows cross-compilation image
│
├── flake.nix                    # Nix flake for dev environment
├── flake.lock                   # Nix flake lock file
├── vendir.tmpl.yml              # Vendir template for file syncing
├── .envrc                       # direnv configuration
├── .gitignore
└── README.md                    # Project documentation
```

## Critical Directories

| Directory | Purpose | Sync Target |
|-----------|---------|-------------|
| `shared/actions/` | GitHub Actions workflows | `.github/workflows/vendor/` |
| `shared/ci/tasks/` | Concourse CI scripts | `ci/vendor/tasks/` |
| `shared/ci/config/` | Shared configurations | `ci/vendor/config/` |
| `images/` | Docker image definitions | Built to us.gcr.io/galoy-org |

## Entry Points

| File | Type | Description |
|------|------|-------------|
| `ci/pipeline.yml` | Pipeline | Main Concourse pipeline definition |
| `ci/values.yml` | Config | Target repos and feature flags |
| `vendir.tmpl.yml` | Template | Controls which files sync to targets |
