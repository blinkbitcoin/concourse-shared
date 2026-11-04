# Plan: Concourse webhook relay over the tailnet

- Status: planned, not started. Start before `ci.blinkbtc.com` becomes tailnet-only.
- Owner: openoms
- Written: 2026-11-04
- Interim: `ci/github-webhooks.py` (proposed separately on branch `ci/github-webhooks`) creates per-repo GitHub hooks. They stop working at the tailnet cutover and are removed in phase 7.

## Why

On 2026-11-04 about 20% of the git resources in team `dev` (54 of 260)
failed with `Could not read from remote repository`. The failures clustered
in check bursts (about 90 checks in 90 seconds, about half failing). Over the
same window, credential-free SSH probes from the same workers and containers
all completed key exchange (120 of 120 in the overlap test). The network is
healthy. The failures happen after key exchange, when every resource
authenticates with the shared `github-blinkbitcoin` key while polling every
minute. Most likely GitHub is limiting that key or account. Webhooks plus
`check_every: 1h` remove the polling load.

Hook audit, same day: no repo had a hook for `ci.blinkbtc.com`. Existing hooks
targeted `ci.blink.sv` (405) or `ci.galoy.io` (404). `charts` (66 resources)
and `private-charts` (31) exceed GitHub's limit of 20 hooks per repo and event.

Once Concourse is reachable only over Tailscale, GitHub's hook senders
(`hooks` ranges in `api.github.com/meta`) can no longer deliver.

## Design

A shared GitHub Actions workflow runs on push, joins the tailnet as an
ephemeral tagged node, and calls only the Concourse check webhooks whose
branch and `paths`/`ignore_paths` match the pushed changes.

```
push -> workflow (environment: concourse-webhooks)
     -> tailscale/github-action (OAuth client, tag:concourse-webhook-relay, ephemeral)
     -> git diff --name-only $before $after -- <paths> ':!<ignore_paths>' per resource
     -> POST https://ci.blinkbtc.com/api/v1/teams/T/pipelines/P/resources/R/check/webhook?webhook_token=...
```

- **Workflow source.** `shared/actions/concourse-webhooks.yml` in concourse-shared,
  synced to every repo's `.github/workflows/` by the existing `bump-shared-files`
  jobs (`ci/tasks/bump-shared-files.sh` copies `vendor/` up a level).
- **Desired state.** Extend `ci/github-webhooks.py` with a `vars` command that
  writes a `CONCOURSE_WEBHOOKS` repository variable per repo: a JSON list of
  `{team, pipeline, resource, branch, paths, ignore_paths}` from the live
  pipelines. Same source of truth and same team scoping as the hook script.
  Repo variables are visible only to collaborators, so pipeline topology
  stays out of public repos. Rerun after repipes; `plan` reports drift.
- **Matching.** Use git pathspecs so matching follows the git resource's own
  `paths`/`ignore_paths` semantics. Ping only resources whose `branch` equals
  the pushed ref. For a new branch (`before` is all zeros) or an unreachable
  `before` after a force-push, ping every resource on that branch. Ping
  sequentially; the 1-hour poll catches anything missed.
- **Secrets.** A GitHub environment `concourse-webhooks`, limited to the
  branches Concourse watches (`main` plus bot branches), holds the
  `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` secrets and one
  `CONCOURSE_WEBHOOK_TOKEN_<TEAM>` secret per team. Branch restriction stops
  a workflow pushed on another branch from reading them. Never echo tokens or
  URLs that contain them.
- **Tailnet policy** (`blink-deployments/tailnet-config/policy.json`, applied by
  its approved pipeline): add `tag:concourse-webhook-relay` to `tagOwners`,
  grant it `tcp:443` to the Concourse web node's tailnet tag only, and add
  policy tests that allow 443 and deny 22, 2222, 5432 and 8200.
- **Fallback.** Keep `check_every: 1h` on every webhook resource. Never use
  `never`.

## Phases

Each phase is one Task under a blink-wip Epic and one PR where code changes.

0. **Prerequisite.** The tailnet-only design for `ci.blinkbtc.com` is fixed:
   which node serves it, its tag, and how runners resolve the name (MagicDNS
   or split DNS).
1. **Tailnet policy PR.** Add the tag, the grant and the tests; apply through
   the tailnet pipeline.
2. **Credentials** (operator, outside Git). Create a Tailscale OAuth client
   restricted to `tag:concourse-webhook-relay` with auth-key write only.
   Create the `concourse-webhooks` environment and its secrets in each repo,
   or in the org if an owner sets selected-repo access.
3. **`vars` command.** Add it to `ci/github-webhooks.py` with tests for
   rendering, team scoping, drift and redaction.
4. **Shared workflow.** Write it plus a test of the matching logic against
   fixture diffs: paths, ignore_paths, branch mismatch, a new branch and a
   force-push.
5. **Pilot on `blink-nostr`** while Concourse is still public, with its GitHub
   hooks removed. Accept when a push triggers exactly the expected check
   builds within 2 minutes and no others.
6. **Rollout.** Run `vars` for all repos and merge the bump-shared-files PRs.
   For `charts` and `private-charts`, first add `webhook_token` to their chart
   resources (`ci/pipeline-fragments.lib.yml` in each repo) and repipe.
7. **Remove GitHub hooks.** Add an explicit `remove` mode to the hook script
   (it currently recreates hooks for every `webhook_token` resource), then
   delete all hooks targeting `ci.blinkbtc.com`.
8. **Cutover.** Make `ci.blinkbtc.com` tailnet-only. Push a test commit to
   each pilot repo and confirm checks trigger.
9. **Measure.** Rerun the git-resource survey. Expect no burst failures and
   `Could not read from remote repository` close to zero.

## Verification and rollback

- Verify each phase with check-build evidence from `fly -t ciblink resources`
  and `watch`, not with green workflow runs alone.
- Roll back the relay by disabling the workflow. Resources fall back to hourly
  polling, or set `check_every` back to the default for minute polling.
- Roll back the cutover by restoring public ingress for the webhook path only,
  allowlisted to GitHub's `hooks` ranges, and recreating hooks with the script.

## Open questions

- The Concourse web node's tailnet tag and DNS (phase 0).
- Whether team `main` (production `blink-deployments`) gets the relay, and its token.
- Actions minutes for private repos: about 30 to 60 seconds per push.
- Whether to automate `vars` from a Concourse job instead of an operator run.
- `GaloyMoney/blink-cli` (formerly `galoy-cli`) and `GaloyMoney/concourse-shared` are
  archived, but pipelines still poll them. Retire those resources.
