# concourse-shared Documentation

## Project Overview

- **Type:** Monolith (CI/CD Infrastructure)
- **Primary Language:** Shell (Bash)
- **Architecture:** Pipeline-as-Code with shared task distribution

## Quick Reference

- **Tech Stack:** Concourse CI, ytt, Nix, Docker, GCP
- **Entry Point:** `ci/pipeline.yml`
- **Architecture Pattern:** Centralized CI resource distribution

## Generated Documentation

- [Project Overview](./project-overview.md)
- [Architecture](./architecture.md)
- [Source Tree Analysis](./source-tree-analysis.md)
- [Development Guide](./development-guide.md)

## Existing Documentation

- [README](../README.md) - Original project documentation

## Getting Started

1. Clone: `gh repo clone blinkbitcoin/concourse-shared`
2. Enter dev env: `nix develop`
3. Add new repo: Edit `ci/values.yml`, run `ci/repipe`

## Key Files

| File | Purpose |
|------|---------|
| `ci/pipeline.yml` | Main Concourse pipeline |
| `ci/values.yml` | Target repos and features |
| `shared/ci/tasks/` | Shared CI scripts |
| `shared/actions/` | GitHub Actions workflows |
| `images/` | Docker image definitions |
