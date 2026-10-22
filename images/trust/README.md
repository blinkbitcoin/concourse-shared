# Shared image trust qualification

Task: https://github.com/blinkbitcoin/blink-wip/issues/1699

## Plan and rationale

Use digest-pinned Debian 12 bases and refresh `ca-certificates` before any
installer downloads. Require at least `20250419~deb12u2`, all twelve published
eMudhra chain fixtures to fail for certificate distrust, and a healthy Debian
request. Repeat verification after tool installation. Timeouts, DNS failures,
expired certificates, and hostname mismatches do not pass the gate. The public
fixture service must be available when building; an outage blocks a build.

Node uses its own verifier: pin Node 24.22.1 and check it independently, including
a healthy npm request. The Rust base includes rustup 1.29.2; check its actual
update client against all twelve fixtures and its normal update servers. Move
the release image from Python 3.8/Buster to Python 3.11/Bookworm and update both
pip's bundled roots and standalone certifi. Configure gcloud's
`core/custom_ca_certs_file` to use the qualified system bundle.

These checks cover the declared verifiers, not every binary's embedded trust
store or custom CA configuration. The pre-existing Go tool pins are preserved;
removing roots from the system store is not equivalent to adding the fixed Go
SPKI blocklist to those binaries. Inventory and qualify every deployed consumer,
including custom roots, before closing the task. Do not treat an image rebuild
or a source merge as rollout evidence.

## Local verification

Build from the repository root, not an individual image directory. The shared
gate is part of every image's Git resource path filter and build context.

```sh
python3 -m unittest discover -s tests -v  # requires ytt in PATH
python3 -m coverage run --branch --source=images/trust -m unittest discover -s tests
python3 -m coverage report --fail-under=100
ytt -f ci/pipeline.yml -f ci/values.yml > ci/build/pipeline.yml
fly validate-pipeline --config ci/build/pipeline.yml

docker --context desktop-linux build --platform linux/amd64 --pull --no-cache \
  -f images/nodejs-concourse/Dockerfile -t ci-node-trust:local .
docker --context desktop-linux run --rm --platform linux/amd64 ci-node-trust:local \
  /usr/bin/python3 /usr/local/lib/ci-trust/verify.py verify --node
```

Repeat the build with `images/release/Dockerfile`,
`images/rust-concourse/Dockerfile`, and `images/wincross/Dockerfile`; use
`verify --rustup` for the Rust images and `verify` for release. `--target
trust-base` tests admission without installing the remaining tools. Full image
builds and consuming tasks are still needed to qualify compatibility.

`ci/repipe` changes a live pipeline; it is not the local rendering command.
The renderer explicitly lists its source files to avoid reading its previous
generated output. The checked-in pipeline is regenerated from current values,
including previously unrendered consumer entries. Review those entries before
applying it to a live pipeline.

## Artifact adoption

1. Build and test the exact reviewed commit with cache disabled. Record base
   digests, package/runtime versions, `image/digest` from Kaniko, and the digest
   returned by the registry publication step. Retain build evidence separately;
   the task output directory alone is not durable evidence.
2. Qualify actual Go/Rust clients and every independent bundled store, not just
   curl. When a client uses custom roots, test that configuration too. Replace
   affected Go binaries with builds using Go 1.26.10/1.27.3 or a later fixed
   release; replacing the OS bundle does not rebuild a binary.
3. Check healthy external dependencies and required internal CA/mTLS paths
   with their actual consumers. Use secret-manager lookups for any credentials;
   never send real credentials to the public negative-test fixtures.
4. Obtain target-specific rollout authorization, publish the qualified artifacts,
   and adopt their exact digests in the owning consumers. The existing `latest`
   publication tags remain compatibility aliases, not immutable adoption proof.
5. Re-run verification in the deployed check/get/put/task containers, including
   long-running or cached containers. Keep the previous qualified digest for
   rollback and record the rollback's trust consequences.

No production image publication, pipeline application, or consumer rollout is
performed by the local commands above.

## References

- https://emudhra.we-have-a-problem.com/
- https://nodejs.org/en/blog/release/v24.22.1
- https://github.com/nodejs/node/pull/66871
- https://docs.cloud.google.com/sdk/gcloud/reference/config
