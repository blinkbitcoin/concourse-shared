# Project Overview

## concourse-shared

Shared CI/CD resources for the blinkbitcoin GitHub organization.

## Purpose

Centralizes CI tasks, GitHub Actions, and Docker images to ensure consistency across multiple repositories. Changes made here automatically propagate to all target repos via Concourse CI pipelines.

## Quick Reference

| Attribute | Value |
|-----------|-------|
| **Type** | CI/CD Infrastructure |
| **Platform** | Concourse CI |
| **Language** | Shell (Bash), YAML |
| **Templating** | ytt (Carvel) |
| **Dev Environment** | Nix Flakes |
| **Container Registry** | us.gcr.io/galoy-org |

## Capabilities

### Shared CI Tasks
- Node.js: code checking, security audits, dependency caching (yarn/pnpm)
- Rust: code checking, cargo configuration
- Docker: build environment preparation, image digest bumping
- Charts: Helm integration testing, chart PR automation
- Release: changelog generation, semantic versioning

### Docker Images
- `nodejs-concourse` - Node.js 20 CI runner
- `rust-concourse` - Rust CI runner
- `release-pipeline` - Release automation
- `wincross-rust` - Windows cross-compilation

### Automation
- Daily GitHub org backup to GCS
- Auto-PR creation for shared file updates

## Target Repositories

| Repository | Features |
|------------|----------|
| mavapay-client | nodejs |
| blink-client | nodejs |
| blink-nostr | nodejs, docker, chart |
| blink-fiat | nodejs, docker, chart |
| blink-circles | nodejs |
| blink-card | rust, docker, chart |
| stablesats-rs | rust, docker, chart |

## Links

- [Pipeline](https://ci.blink.sv/teams/dev/pipelines/blink-concourse-shared)
- [GitHub](https://github.com/blinkbitcoin/concourse-shared)
