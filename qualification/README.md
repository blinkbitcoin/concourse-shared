# Published candidate qualification

Task: https://github.com/blinkbitcoin/blink-wip/issues/1814
Acceptance prerequisite: https://github.com/blinkbitcoin/blink-wip/issues/1699

## Plan and rationale

Pull each already-published candidate by digest on a separate Linux CI runner,
record its resolved repository digest, and rerun the artifact's own system and
Node/rustup trust gates. Test installed clients without credentials before
changing consumers. The workflow never rebuilds these images, pushes registry
tags, deploys a service, or uses a production Docker socket.

The four digests in `.github/workflows/candidate-qualification.yml` are the
verified Node5, Rust5, release4 and wincross5 artifacts from 2026-10-24. A new
candidate requires an explicit digest change and new receipts.

## Coverage and evidence

`clients.py` uses a fresh temporary HOME and an allowlisted environment. It
records installed versions, rejection results for all twelve live endpoints,
and healthy TLS controls for Git, GitHub CLI, gcloud, gh-token, Buf, Cargo Audit
and release chart-releaser/Helm/kubectl/Python clients where installed. Certificate expiry,
hostname mismatch, timeout, DNS failure and auth
failure do not count as negative-test success. The artifact's existing gate
also verifies all fourteen pinned published certificate-chain fixtures offline.

Before starting a disposable candidate container, the workflow resolves each
negative hostname once and passes explicit Docker `--add-host` mappings. For
every client/hostname attempt, independent OpenSSL connections to that pinned
IPv4 peer compare the complete served chain (ordered DER SHA-256 fingerprints)
with the checksum-verified published fixture immediately before and after the
client command. Missing, changed, malformed or unexpected chains fail the result;
a failed preflight does not run the client. Receipts retain both observations.
This is independent per-attempt chain identity evidence, not an interception of
the client's own TLS session. It cannot rule out a server deliberately serving
different chains selectively to different clients. No trust store is changed.
The host mappings exist only inside the disposable test container.

GitHub CLI requires a token to attempt its API command. Both its negative
requests and healthy control use a literal non-secret dummy token. The healthy
GitHub API's explicit HTTP401 Bad credentials response proves TLS connectivity
only. It does not qualify authenticated GitHub access. No real credentials are
sent to public test endpoints.

gcloud also uses a literal dummy access token and the image's configured system
CA path. Storage retries are disabled so an expected certificate rejection is
reported directly instead of exhausting the probe timeout. Its storage endpoint
override exercises the installed SDK against each
negative fixture; the normal API's explicit HTTP401 response is a TLS-only
control. gh-token lists installations using a temporary synthetic RSA key that
belongs to no GitHub App. Its expected invalid-JWT response is likewise TLS-only.
No token is generated, revoked or accepted by a real account. Buf reads a public
protobuf source archive, and Cargo Audit fetches the public advisory database
against an empty synthetic lockfile with yanked-crate lookup disabled.

The pinned gh-token release writes its own errors to stdout, so its negative
probe requires the local transport-error prefix as well as certificate distrust.
It never prints non-200 response bodies. Its healthy control accepts only its
exact 401/404 status diagnostic for the nonexistent synthetic app; this proves
an HTTPS response, not authentication. Cargo Audit's generic I/O error remains
inconclusive and fails qualification when its certificate cause is hidden.

Chart-releaser runs its actual `index` command against a local public-repository
fixture and synthetic chart package. It tests the GitHub release-read endpoint
with `--git-base-url`; the healthy control reads the existing upstream v1.6.0
release. A successful index operation records `healthy_operation_succeeded:
true`. The exact GitHub API rate-limit diagnostic for that release URL also
establishes `healthy_tls: true`, but records `healthy_operation_succeeded: false`;
it is not release-read success. Other 403 responses and unrelated endpoints fail.
This permits shared unauthenticated runner quota exhaustion without weakening
negative certificate tests. Neither `--push` nor `--pr` is set, so it creates no remote release,
commit or pull request. The local fixture ref is discarded with the temporary
directory. This does not qualify release uploads or authenticated private APIs.

Basic tooling smoke checks execute Node, check release Python dependencies,
compile native Rust, and cross-compile a minimal Windows program. These are
tooling checks, not substitutes for actual consuming-repository test suites.

`node-consumer-canary.yml` additionally checks out the public `blink-client`
consumer at `393276b690b8f809f068cfb3e3f2afc54ff91e76`, without persisted GitHub
credentials, and runs its dependency installation, Jest tests and tsup build in
the exact Node candidate. This exercises the consumer commands without its
release/publish steps. It records the checked-out commit and resolved image;
the live Concourse pipeline still requires its own adoption and readback.

Both workflows rerun when the candidate matrix changes. A contract assertion
requires their Node image digests to agree. Trust-source changes also trigger
the matrix because the probe imports the published host/chain inputs.

CI retains resolved digests, peer mappings, artifact trust output and installed-client JSON
receipts even on failure. A failed check holds adoption; it never promotes an
alias or retries a production operation.

## Remaining consumer gates

This workflow does not certify every embedded client, Go build version, custom
root override, authenticated registry operation or private CA/mTLS path. In
particular, Docker/Compose and other independently networking
Cargo subcommands require an applicability inventory and actual
consumer checks. A patched OS bundle does not add Go's compromised-key blocklist
to old binaries. Replace affected binaries where those remaining checks require
it; do not infer their qualification from Git or curl.

Inventory consumers and competing image publishers before alias promotion.
Adopt verified digests through each source owner's rollout and record the image
actually resolved in the new build. Keep #1699 open until its runtime and
consumer criteria are satisfied. Do not use container intercept on running
Concourse builds to collect this evidence.

Command references: [gcloud endpoint overrides](https://docs.cloud.google.com/sdk/gcloud/reference/topic/endpoint-override),
[gcloud token overrides](https://docs.cloud.google.com/sdk/docs/authenticate),
[gh-token](https://github.com/Link-/gh-token), [Buf HTTPS inputs](https://buf.build/docs/reference/inputs/),
[Cargo Audit options](https://github.com/rustsec/rustsec/blob/main/cargo-audit/src/commands/audit.rs).

## Local tests

```sh
python3 -m unittest discover -s tests -p 'test_candidate*.py' -v
python3 -m coverage run --branch --source=qualification -m unittest discover -s tests -p 'test_candidate*.py'
python3 -m coverage report --fail-under=100
```
