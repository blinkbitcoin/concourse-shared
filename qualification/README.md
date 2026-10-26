# Published candidate qualification

Task: https://github.com/blinkbitcoin/blink-wip/issues/1814
Acceptance prerequisite: https://github.com/blinkbitcoin/blink-wip/issues/1699

## Plan and rationale

Pull each already-published candidate by digest on a separate Linux CI runner,
record its resolved repository digest, and rerun the artifact's own system and
Node/rustup trust gates. Test installed clients without credentials before
changing consumers. The workflow never rebuilds these images, pushes registry
tags, deploys a service, or uses a production Docker socket.

The matrix is the source of the selected digests. This provenance table is
checked against the parsed matrix; every replacement needs new receipts.
Publication dates below are UTC.

| Profile | Repository | Digest | Publication |
| --- | --- | --- | --- |
| node | nodejs-concourse | sha256:8e9d5591bc55697df061390c745c422958370cf714e00249b78d4784fee7d6a6 | Node5, 2026-10-24 |
| rust | rust-concourse | sha256:e3efda4c461ed0ca5d341b8dc8635f0f953a6e82dee5a7826ac5eb96ab3d98ed | Rust6, 2026-10-26, source 60171adb71a376013d23af9568b16a03669c8fc8 |
| release | release-pipeline | sha256:7d6b62ac98f82b5ab8d66a9652e21e8c68506d63f3c9ce68ab5d1bbef87c22d6 | release4, 2026-10-24 |
| wincross | wincross-rust | sha256:8a7a354f24f541bc1330f5d207acb0cabcf8f5a1d30178313876e36f9c751718 | wincross5, 2026-10-24 |

Rust6 includes PR #43's Cargo Audit transport fix. Publication evidence:
[Concourse Rust build 6](https://ci.blinkbtc.com/teams/dev/pipelines/blink-concourse-shared/jobs/build-rust-concourse-image/builds/6).

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
a failed preflight does not run the client. Each snapshot also verifies signatures,
links, current validity, server purpose and hostname with OpenSSL, trusting only
the final observed certificate for that separate integrity control. Expired or
not-yet-valid chains cannot qualify through Git/GnuTLS's generic error text.
Receipts retain both observations and the validity result.
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
true`. The direct GitHub API `403 API rate limit exceeded` diagnostic for that release URL also
establishes `healthy_tls: true`, but records `healthy_operation_succeeded: false`;
it is not release-read success. Cached `not making remote request` quota errors
are inconclusive and fail: they do not prove that this attempt performed TLS.
Other 403 responses and unrelated endpoints fail.
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

The `pip` client runs the installed interpreter's real `pip download` command,
which uses pip's normal session and default trust-store selection. It downloads
only wheels, without dependencies, caching, configuration files or installation.
The healthy control downloads pinned `pip==26.2.2` from PyPI; negative controls
use a separate destination and a nonexistent package on each affected index.
Pip receipts retain both streams: its skipped-index TLS diagnostic is on stdout,
while the final package-resolution error is on stderr. A resolution error alone
never qualifies. The separate `certifi` client exercises the standalone bundle. See
[pip's certificate-store documentation](https://pip.pypa.io/en/stable/topics/https-certificates/).

Both workflows rerun when the candidate matrix changes. A contract assertion
requires their Node image digests to agree. Trust-source changes also trigger
the matrix because the probe imports the published host/chain inputs.

CI retains resolved digests, peer mappings, artifact trust output and installed-client JSON
receipts even on failure. A failed check holds adoption; it never promotes an
alias or retries a production operation.

## Drift, expiry and bounded failures

The candidate matrix also runs weekly (Monday 07:43 UTC) and supports manual
workflow dispatch after merge. Contracts enforce 100% statement/branch coverage
and must pass before any candidate job starts.

Before client loops, a global preflight checks each pinned peer once. It fails
immediately with a `chain-preflight` receipt on an unavailable, changed or invalid
chain, or one that will not remain valid for the next 60 days. Per-attempt
before/after checks remain mandatory; later chain drift aborts with the failing
client and complete probe. Setup errors produce a `qualification-inputs` receipt.
An 18-minute monotonic qualification deadline bounds every subprocess timeout
and emits `qualification-deadline` before the 30-minute job budget; the normal
60-second subprocess cap remains in effect. Failed qualification still runs the
bounded smoke check. These receipts report failure, never a successful probe.

To refresh fixtures after a scheduled drift/expiry failure:

1. Obtain the updated published chain archive from the public fixture operator
   linked in `images/trust/README.md`. Independently review the full chains,
   affected issuer keys, hostnames, validity and expected cross-sign variants.
2. Update the canonical `FIXTURE_ZIP` and `FIXTURE_SHA256` in
   `images/trust/verify.py`, its hosts if needed, and the documented fixed offline
   verification time only when required. Keep the historical trust test and the
   current-time live validity check distinct. Do not accept an arbitrary renewed
   or intercepted chain just to make a check green.
3. Regenerate the sanitized public transcript test fixture, run contracts and
   all image trust tests, then review the exact fixture diff. This shared input
   triggers both image build gates and candidate qualification.
4. Run the matrix against the selected digests and retain fresh receipts. If an
   artifact itself needs rebuilding, follow the separate publication/adoption
   gates and update both the matrix and provenance table. Never relax chain
   matching or expiry checks to bypass a stale fixture.

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
python3 -m pip install -r qualification/requirements-test.txt
python3 -m unittest discover -s tests -p 'test_candidate*.py' -v
python3 -m coverage run --branch --source=qualification -m unittest discover -s tests -p 'test_candidate*.py'
python3 -m coverage report --fail-under=100
```
