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
and healthy TLS controls for Git, GitHub CLI and release Helm/kubectl/Python
clients. Certificate expiry, hostname mismatch, timeout, DNS failure and auth
failure do not count as negative-test success. The artifact's existing gate
also verifies all fourteen pinned published certificate-chain fixtures offline.

GitHub CLI requires a token to attempt its API command. Both its negative
requests and healthy control use a literal non-secret dummy token. The healthy
GitHub API's explicit HTTP401 Bad credentials response proves TLS connectivity
only. It does not qualify authenticated GitHub access. No real credentials are
sent to public test endpoints.

Basic tooling smoke checks execute Node, check release Python dependencies,
compile native Rust, and cross-compile a minimal Windows program. These are
tooling checks, not substitutes for actual consuming-repository test suites.

`node-consumer-canary.yml` additionally checks out the public `blink-client`
consumer at `393276b690b8f809f068cfb3e3f2afc54ff91e76`, without persisted GitHub
credentials, and runs its dependency installation, Jest tests and tsup build in
the exact Node candidate. This exercises the consumer commands without its
release/publish steps. It records the checked-out commit and resolved image;
the live Concourse pipeline still requires its own adoption and readback.

CI retains resolved digests, artifact trust output and installed-client JSON
receipts even on failure. A failed check holds adoption; it never promotes an
alias or retries a production operation.

## Remaining consumer gates

This workflow does not certify every embedded client, Go build version, custom
root override, authenticated registry operation or private CA/mTLS path. In
particular, buf, Docker/Compose, chart-releaser, gh-token, gcloud and independently
networking Cargo subcommands require an applicability inventory and actual
consumer checks. A patched OS bundle does not add Go's compromised-key blocklist
to old binaries. Replace affected binaries where those remaining checks require
it; do not infer their qualification from Git or curl.

Inventory consumers and competing image publishers before alias promotion.
Adopt verified digests through each source owner's rollout and record the image
actually resolved in the new build. Keep #1699 open until its runtime and
consumer criteria are satisfied. Do not use container intercept on running
Concourse builds to collect this evidence.

## Local tests

```sh
python3 -m unittest discover -s tests -p test_candidate_clients.py -v
python3 -m coverage run --branch --source=qualification -m unittest discover -s tests -p test_candidate_clients.py
python3 -m coverage report --fail-under=100
```
