#!/usr/bin/env python3
"""Reconcile GitHub push webhooks with the Concourse resources that accept them.

Desired state is read from the live pipelines of one fly target: every git
resource with a `webhook_token` gets one push hook on its GitHub repository,
pointing at that resource's check webhook on the target's Concourse.

  plan    read-only (default); needs no token
  apply   create/update hooks after confirmation; --prune also deletes this
          team's duplicate or orphaned hooks and every check webhook aimed at
          a --prune-host
  verify  read the latest delivery of every hook managed for this target

Hooks for other teams on the same Concourse, and for hosts not named with
--prune-host, are never touched.

The token is read from CONCOURSE_WEBHOOK_TOKEN or a hidden prompt, is sent to
GitHub over stdin only, and is redacted from all output.
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

GITHUB_HOOK_LIMIT = 20
WEBHOOK_PATH = re.compile(
    r"^/api/v1/teams/(?P<team>[^/]+)/pipelines/(?P<pipeline>[^/]+)"
    r"/resources/(?P<resource>[^/]+)/check/webhook$")
REPO_URI = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$")


def run(cmd, stdin=None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=True).stdout


def redact(url):
    return re.sub(r"(webhook_token=)[^&]*", r"\1REDACTED", url)


def hook_key(url):
    """(host, path) of a Concourse check webhook URL, or None for other hooks."""
    parts = urlsplit(url)
    if not WEBHOOK_PATH.match(parts.path):
        return None
    return parts.scheme + "://" + parts.netloc, parts.path


def hook_token(url):
    match = re.search(r"webhook_token=([^&]*)", url)
    return match.group(1) if match else None


def repo_slug(uri):
    match = REPO_URI.match(uri or "")
    return match.group("slug") if match else None


class Reconciler:
    def __init__(self, target, runner=None, out=None, prune_hosts=()):
        self.target, self.run, self.out = target, runner or run, out or sys.stdout
        self.prune_hosts = {host.rstrip("/") for host in prune_hosts}
        self.api, self.team = self.target_info()

    def say(self, *parts):
        print(*parts, file=self.out)

    def fly(self, *args):
        return self.run(["fly", "-t", self.target, *args])

    def gh(self, *args, stdin=None):
        return self.run(["gh", "api", *args], stdin=stdin)

    def target_info(self):
        for line in self.fly("targets").splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == self.target:
                return fields[1].rstrip("/"), fields[2]
        raise SystemExit(f"fly target {self.target!r} not found")

    def desired(self):
        """{repo slug as configured: [(pipeline, resource)]} plus skipped resources."""
        wanted, skipped = {}, []
        for pipeline in json.loads(self.fly("pipelines", "--json")):
            name = pipeline["name"]
            if pipeline.get("archived"):
                continue
            if pipeline.get("instance_vars"):
                skipped.append(f"{name}: instanced pipeline not inspected; Concourse webhooks cannot target instances")
                continue
            config = json.loads(self.fly("get-pipeline", "-p", name, "--json"))
            for resource in config.get("resources", []):
                if not resource.get("webhook_token"):
                    continue
                slug = repo_slug((resource.get("source") or {}).get("uri"))
                if resource.get("type") != "git" or not slug:
                    skipped.append(f"{name}/{resource['name']}: not a GitHub git resource")
                    continue
                wanted.setdefault(slug, []).append((name, resource["name"]))
        return wanted, skipped

    def repo(self, slug):
        info = json.loads(self.gh(f"repos/{slug}"))
        return info["full_name"], info.get("archived", False), info.get("permissions", {}).get("admin", False)

    def hooks(self, repo):
        out = self.gh(f"repos/{repo}/hooks", "--paginate", "--jq", ".[]")
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def path(self, pipeline, resource):
        return f"/api/v1/teams/{self.team}/pipelines/{pipeline}/resources/{resource}/check/webhook"

    def managed(self, key):
        """Our host and team, or another host the operator explicitly retired."""
        if key[0] == self.api:
            return WEBHOOK_PATH.match(key[1]).group("team") == self.team
        return key[0] in self.prune_hosts

    def plan(self, prune=False, token=None):
        """Return (actions, blocked). Actions are dicts; blocked are messages."""
        wanted, skipped = self.desired()
        blocked = list(skipped)
        repos = {}
        for slug, resources in sorted(wanted.items()):
            try:
                full, archived, admin = self.repo(slug)
            except subprocess.CalledProcessError:
                blocked.append(f"{slug}: repository not readable")
                continue
            if archived:
                blocked.append(f"{full}: archived; {len(resources)} resource(s) still reference it")
                continue
            if not admin:
                blocked.append(f"{full}: no admin permission")
                continue
            repos.setdefault(full, []).extend(resources)

        actions = []
        for repo, resources in sorted(repos.items()):
            existing = self.hooks(repo)
            want = {(self.api, self.path(p, r)) for p, r in resources}
            seen = set()
            repo_actions = []
            for hook in existing:
                current = hook.get("config", {}).get("url", "")
                key = hook_key(current)
                if key is None or not self.managed(key):
                    continue
                if key in want and key not in seen:
                    seen.add(key)
                    stale_token = token is not None and hook_token(current) != token
                    if not hook.get("active") or "push" not in hook.get("events", []) or stale_token:
                        repo_actions.append({"op": "update", "repo": repo, "id": hook["id"], "path": key[1]})
                    continue
                reason = "duplicate" if key in want else (
                    "retired Concourse host" if key[0] != self.api else "resource no longer accepts webhooks")
                if prune:
                    repo_actions.append({"op": "delete", "repo": repo, "id": hook["id"],
                                         "url": redact(current), "reason": reason})
                else:
                    blocked.append(f"{repo}: hook {hook['id']} {redact(current)} ({reason}); use --prune")
            for host, path in sorted(want - seen):
                repo_actions.append({"op": "create", "repo": repo, "path": path})
            total = len(existing) + sum(1 for a in repo_actions if a["op"] == "create") \
                - sum(1 for a in repo_actions if a["op"] == "delete")
            if total > GITHUB_HOOK_LIMIT:
                blocked.append(f"{repo}: would have {total} hooks, above GitHub's {GITHUB_HOOK_LIMIT}; skipped")
                continue
            actions.extend(repo_actions)
        return actions, blocked

    def show(self, actions, blocked):
        for a in actions:
            target = a.get("url") or self.api + a["path"]
            extra = f" ({a['reason']})" if a.get("reason") else ""
            self.say(f"{a['op']:7} {a['repo']} {target}{extra}")
        for message in blocked:
            self.say(f"BLOCKED {message}")
        self.say(f"{len(actions)} change(s), {len(blocked)} blocked")

    def check_token(self, path, token):
        request = urllib.request.Request(f"{self.api}{path}?webhook_token={token}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError:
            return False

    def apply(self, actions, token):
        for a in actions:
            if a["op"] == "delete":
                self.gh("-X", "DELETE", f"repos/{a['repo']}/hooks/{a['id']}")
            else:
                body = json.dumps({"name": "web", "active": True, "events": ["push"],
                                   "config": {"url": self.api + a["path"] + f"?webhook_token={token}",
                                              "content_type": "json", "insecure_ssl": "0"}})
                endpoint = f"repos/{a['repo']}/hooks" + (f"/{a['id']}" if a["op"] == "update" else "")
                method = "PATCH" if a["op"] == "update" else "POST"
                self.gh("-X", method, endpoint, "--input", "-", stdin=body)
            self.say(f"done    {a['op']} {a['repo']} {a.get('url') or self.api + a['path']}")

    def verify(self):
        wanted, _ = self.desired()
        failures = 0
        for slug in sorted(wanted):
            try:
                repo, archived, admin = self.repo(slug)
            except subprocess.CalledProcessError:
                continue
            if archived or not admin:
                continue
            for hook in self.hooks(repo):
                key = hook_key(hook.get("config", {}).get("url", ""))
                if not key or key[0] != self.api or not self.managed(key):
                    continue
                deliveries = json.loads(self.gh(f"repos/{repo}/hooks/{hook['id']}/deliveries?per_page=1"))
                code = deliveries[0].get("status_code") if deliveries else None
                ok = code is not None and 200 <= code < 300
                failures += not ok
                self.say(f"{'ok' if ok else 'FAIL':7} {repo} {self.api}{key[1]} last_delivery={code}")
        return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="plan", choices=["plan", "apply", "verify"])
    parser.add_argument("-t", "--target", default=os.environ.get("FLY_TARGET", "ciblink"))
    parser.add_argument("--prune", action="store_true",
                        help="delete orphaned and duplicate hooks for this team, and hooks on --prune-host")
    parser.add_argument("--prune-host", action="append", default=[], metavar="URL",
                        help="retired Concourse whose check webhooks are managed for deletion (repeatable)")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    args = parser.parse_args(argv)

    reconciler = Reconciler(args.target, prune_hosts=args.prune_host)
    reconciler.say(f"target {args.target}: {reconciler.api} team {reconciler.team}")
    if args.command == "verify":
        return 1 if reconciler.verify() else 0

    token = None
    if args.command == "apply":
        token = os.environ.get("CONCOURSE_WEBHOOK_TOKEN") or getpass.getpass("Concourse webhook token: ")
        if not token:
            raise SystemExit("no token provided")
    actions, blocked = reconciler.plan(prune=args.prune, token=token)
    reconciler.show(actions, blocked)
    if args.command == "plan" or not actions:
        return 0

    probe = next((a["path"] for a in actions if a["op"] != "delete"), None)
    if probe and not reconciler.check_token(probe, token):
        raise SystemExit(f"token rejected by {reconciler.api}{probe}; nothing changed")
    if not args.yes and input(f"Apply {len(actions)} change(s)? Type 'apply': ").strip() != "apply":
        raise SystemExit("aborted; nothing changed")
    reconciler.apply(actions, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
