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

The checked-in lock starts from Cargo Audit 0.22.2's published `Cargo.lock`.
Only these required dependency changes are allowed:

- `gix-transport` 0.57.1 → the path-patched 0.57.2.
- `gix-packetline` 0.21.4 → 0.21.5 and `reqwest` 0.13.3 → 0.13.5.
- Add `base64` 0.23.1 for reqwest. Transport and unchanged `hyper-util` keep
  base64 0.22.1, now explicitly disambiguated in their dependency records.

`lock-delta.json` records the exact removed/added package records, including
checksums and dependencies, and a canonical hash of the published baseline.
Before replacing the extracted lock, every image build runs `verify-lock.py`
against that checksum-authenticated baseline. Unrelated versions, dependency
edges, sources, checksums, duplicate records and lock metadata changes fail.

To refresh intentionally, start in a fresh absolute work directory so
`prepare.sh` extracts the published lock again. With Cargo 1.97.1:

```sh
sh prepare.sh /absolute/work/path
cp /absolute/work/path/cargo-audit-0.22.2/Cargo.lock /absolute/work/path/Cargo.lock.published
cargo update --manifest-path /absolute/work/path/cargo-audit-0.22.2/Cargo.toml \
  --package gix-transport@0.57.1 --precise 0.57.2 \
  --config 'patch.crates-io.gix-transport.path="/absolute/work/path/gix-transport-0.57.2"'
python3 verify-lock.py /absolute/work/path/Cargo.lock.published \
  /absolute/work/path/cargo-audit-0.22.2/Cargo.lock
```

Copy the updated lock here only after reviewing its delta against the untouched
published lock and running `verify-lock.py PUBLISHED_LOCK Cargo.lock`. Do not
regenerate the whole dependency graph. Any required delta change needs explicit
review of `lock-delta.json`; rerun the Linux build and TLS qualification.

The regression workflow builds the actual Linux binary and mounts it into the
previously published Rust runtime. It uses the unchanged, commit-pinned PR #42
harness: every affected endpoint must yield an explicit certificate error with
matching chains before and after the probe, and the healthy Git fetch must pass.
This is build/runtime regression evidence, not publication or consumer adoption.
The complete image also builds with the existing no-push Kaniko workflow.

Tracking: https://github.com/blinkbitcoin/blink-wip/issues/1824
