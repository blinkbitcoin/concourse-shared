#!/bin/sh
# Regenerate a registry-compatible crate from unchanged canonical source.
# cargo package removes sibling path dependencies, avoiding duplicate gix types.
set -eu
work=$1
mkdir -p "$work"
cd "$work"
curl --fail --silent --show-error --location --retry 3 \
  https://codeload.github.com/GitoxideLabs/gitoxide/tar.gz/1228b872bd6f8d9c057db3c7250c28843e8b05cf \
  -o transport.tar.gz
curl --fail --silent --show-error --location --retry 3 \
  https://static.crates.io/crates/cargo-audit/cargo-audit-0.22.2.crate \
  -o audit.tar.gz
printf '%s\n' \
  'd3ad34dab1badf1e3b99f91904ea8e9f576af97aeefae822dec2c27d44a1cbeb  transport.tar.gz' \
  '700c2b240f7fd330c24b675fe429f73a5b676531fcc6300400b2b67f155ba12a  audit.tar.gz' \
  | sha256sum -c -
tar -xzf transport.tar.gz
tar -xzf audit.tar.gz
cargo package --manifest-path gitoxide-1228b872bd6f8d9c057db3c7250c28843e8b05cf/gix-transport/Cargo.toml \
  --no-verify --allow-dirty --exclude-lockfile --target-dir "$work/package-target"
tar -xzf "$work/package-target/package/gix-transport-0.57.2.crate"
