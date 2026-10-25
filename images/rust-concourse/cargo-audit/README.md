# Cargo Audit transport fix

Cargo Audit 0.22.2's reqwest transport converts a request failure to a string,
losing its TLS error source. This made the published Rust candidate's negative
trust probes inconclusive even though its healthy Git fetch passed.

`prepare.sh` verifies both source archive checksums, then regenerates the
`gix-transport` 0.57.2 package from the unchanged upstream fix:
https://github.com/GitoxideLabs/gitoxide/commit/1228b872bd6f8d9c057db3c7250c28843e8b05cf

`cargo package` normalizes the monorepo's sibling path dependencies to registry
dependencies. Cargo's build configuration patches only this normalized crate
into Cargo Audit; no downloaded source is edited. The checked-in lockfile locks
all remaining dependencies. The image installs only the resulting binary, and
other tools retain their original dependency resolution.

To refresh the lockfile intentionally, run `sh prepare.sh /absolute/work/path`
with Cargo 1.97.1, then run `cargo generate-lockfile` against the extracted
Cargo Audit manifest with `--config 'patch.crates-io.gix-transport.path="..."'`
pointing to the generated transport package. Copy the resulting lockfile here.
Review version changes and rerun both image and TLS qualification checks.

The regression workflow builds the actual Linux binary and mounts it into the
previously published Rust runtime. It uses the unchanged, commit-pinned PR #42
harness: every affected endpoint must yield an explicit certificate error with
matching chains before and after the probe, and the healthy Git fetch must pass.
This is build/runtime regression evidence, not publication or consumer adoption.
The complete image also builds with the existing no-push Kaniko workflow.

Tracking: https://github.com/blinkbitcoin/blink-wip/issues/1824
