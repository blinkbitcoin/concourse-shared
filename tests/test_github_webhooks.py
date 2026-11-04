"""Exercise webhook reconciliation against fake fly and gh responses."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
import unittest.mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("github_webhooks", ROOT / "ci/github-webhooks.py")
gw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gw)

API = "https://ci.example.com"
OLD = "https://ci-old.example.com"
TOKEN = "s3cr3t-token"


def check_url(host, team, pipeline, resource, token="x"):
    return f"{host}/api/v1/teams/{team}/pipelines/{pipeline}/resources/{resource}/check/webhook?webhook_token={token}"


def hook(hook_id, url, active=True, events=("push",)):
    return {"id": hook_id, "active": active, "events": list(events), "config": {"url": url}}


class FakeRunner:
    def __init__(self, pipelines, repos, hooks, deliveries=None, extra_pipelines=()):
        self.pipelines, self.repos, self.hooks = pipelines, repos, hooks
        self.deliveries, self.extra_pipelines = deliveries or {}, list(extra_pipelines)
        self.calls = []

    def __call__(self, cmd, stdin=None):
        self.calls.append((cmd, stdin))
        if cmd[:2] == ["fly", "-t"]:
            args = cmd[3:]
            if args == ["targets"]:
                return f"other  {OLD}  dev  never\nci  {API}/  dev  never\n"
            if args == ["pipelines", "--json"]:
                return json.dumps([{"name": name} for name in self.pipelines] + self.extra_pipelines)
            if args[:2] == ["get-pipeline", "-p"]:
                return json.dumps({"resources": self.pipelines[args[2]]})
        if cmd[:2] == ["gh", "api"]:
            args = cmd[2:]
            if len(args) == 1 and args[0].startswith("repos/") and args[0].count("/") == 2:
                slug = args[0][len("repos/"):]
                if slug not in self.repos:
                    raise subprocess.CalledProcessError(1, cmd)
                return json.dumps(self.repos[slug])
            if args[0].endswith("/hooks") and "--paginate" in args:
                repo = args[0][len("repos/"):-len("/hooks")]
                return "".join(json.dumps(h) + "\n" for h in self.hooks.get(repo, []))
            if "/deliveries?" in args[0]:
                hook_id = int(args[0].split("/hooks/")[1].split("/")[0])
                code = self.deliveries.get(hook_id)
                return json.dumps([] if code is None else [{"status_code": code}])
            if args[0] == "-X":
                return "{}"
        raise AssertionError(f"unexpected command {cmd}")

    def mutations(self):
        return [(cmd, stdin) for cmd, stdin in self.calls if cmd[:3] == ["gh", "api", "-X"]]


def resource(name, uri, webhook=True, kind="git"):
    r = {"name": name, "type": kind, "source": {"uri": uri}}
    if webhook:
        r["webhook_token"] = "((webhook.secret))"
    return r


def repo(full_name, archived=False, admin=True):
    return {"full_name": full_name, "archived": archived, "permissions": {"admin": admin}}


class GithubWebhooksTests(unittest.TestCase):
    def reconciler(self, runner, prune_hosts=()):
        return gw.Reconciler("ci", runner=runner, out=io.StringIO(), prune_hosts=prune_hosts)

    def test_parsing_and_redaction(self):
        for uri in ("git@github.com:org/app.git", "ssh://git@github.com/org/app", "https://github.com/org/app.git"):
            self.assertEqual(gw.repo_slug(uri), "org/app")
        self.assertIsNone(gw.repo_slug("git@gitlab.com:org/app.git"))
        self.assertEqual(gw.redact(check_url(API, "dev", "p", "r", TOKEN)),
                         check_url(API, "dev", "p", "r", "REDACTED"))
        self.assertIsNone(gw.hook_key("https://hooks.slack.com/services/x"))

    def test_plan_creates_missing_hooks_and_leaves_foreign_hooks_alone(self):
        runner = FakeRunner(
            {"app": [resource("repo", "git@github.com:org/app.git"),
                     resource("deps", "git@github.com:org/app.git"),
                     resource("polled", "git@github.com:org/app.git", webhook=False)]},
            {"org/app": repo("org/app")},
            {"org/app": [hook(1, check_url(API, "dev", "app", "repo")),
                         hook(2, check_url(API, "main", "app", "repo")),
                         hook(3, check_url(OLD, "dev", "app", "repo")),
                         hook(4, "https://hooks.slack.com/services/x")]})
        actions, blocked = self.reconciler(runner).plan(prune=True)
        self.assertEqual(actions, [{"op": "create", "repo": "org/app",
                                    "path": "/api/v1/teams/dev/pipelines/app/resources/deps/check/webhook"}])
        self.assertEqual(blocked, [])

    def test_retired_host_and_orphans_need_explicit_prune(self):
        runner = FakeRunner(
            {"app": [resource("repo", "git@github.com:org/app.git")]},
            {"org/app": repo("org/app")},
            {"org/app": [hook(1, check_url(API, "dev", "app", "repo", TOKEN)),
                         hook(2, check_url(API, "dev", "gone", "repo", TOKEN)),
                         hook(3, check_url(OLD, "dev", "app", "repo", TOKEN))]})
        actions, blocked = self.reconciler(runner, prune_hosts=[OLD]).plan()
        self.assertEqual(actions, [])
        self.assertEqual(len(blocked), 2)
        self.assertTrue(all("use --prune" in b and TOKEN not in b for b in blocked))

        actions, blocked = self.reconciler(runner, prune_hosts=[OLD]).plan(prune=True)
        self.assertEqual(sorted((a["op"], a["id"]) for a in actions), [("delete", 2), ("delete", 3)])
        self.assertTrue(all(TOKEN not in a["url"] for a in actions))

    def test_inactive_or_stale_token_hooks_are_updated(self):
        runner = FakeRunner(
            {"app": [resource("repo", "git@github.com:org/app.git"),
                     resource("deps", "git@github.com:org/app.git")]},
            {"org/app": repo("org/app")},
            {"org/app": [hook(1, check_url(API, "dev", "app", "repo", TOKEN), active=False),
                         hook(2, check_url(API, "dev", "app", "deps", "old-token"))]})
        actions, _ = self.reconciler(runner).plan()
        self.assertEqual([a["id"] for a in actions], [1])
        actions, _ = self.reconciler(runner).plan(token=TOKEN)
        self.assertEqual(sorted(a["id"] for a in actions), [1, 2])

    def test_archived_unreadable_unadministered_and_full_repos_are_blocked(self):
        hooks = [hook(i, f"https://example.com/{i}") for i in range(gw.GITHUB_HOOK_LIMIT)]
        runner = FakeRunner(
            {"app": [resource("a", "git@github.com:org/archived.git"),
                     resource("b", "git@github.com:org/missing.git"),
                     resource("c", "git@github.com:org/locked.git"),
                     resource("d", "git@github.com:org/full.git"),
                     resource("e", "https://example.com/not-github.git")]},
            {"org/archived": repo("org/archived", archived=True),
             "org/locked": repo("org/locked", admin=False),
             "org/full": repo("org/full")},
            {"org/full": hooks})
        actions, blocked = self.reconciler(runner).plan()
        self.assertEqual(actions, [])
        text = "\n".join(blocked)
        for expected in ("archived", "not readable", "no admin", "above GitHub's 20", "not a GitHub git resource"):
            self.assertIn(expected, text)

    def test_renamed_repositories_are_merged(self):
        runner = FakeRunner(
            {"app": [resource("repo", "git@github.com:old/app.git"),
                     resource("deps", "git@github.com:new/app.git")]},
            {"old/app": repo("new/app"), "new/app": repo("new/app")},
            {})
        actions, _ = self.reconciler(runner).plan()
        self.assertEqual({a["repo"] for a in actions}, {"new/app"})
        self.assertEqual(len(actions), 2)

    def test_apply_sends_token_only_over_stdin(self):
        runner = FakeRunner(
            {"app": [resource("repo", "git@github.com:org/app.git"),
                     resource("deps", "git@github.com:org/app.git")]},
            {"org/app": repo("org/app")},
            {"org/app": [hook(7, check_url(API, "dev", "app", "deps", TOKEN), active=False),
                         hook(8, check_url(OLD, "dev", "app", "repo", TOKEN))]})
        reconciler = self.reconciler(runner, prune_hosts=[OLD])
        actions, _ = reconciler.plan(prune=True, token=TOKEN)
        reconciler.apply(actions, TOKEN)
        mutations = runner.mutations()
        self.assertEqual(sorted(cmd[3] for cmd, _ in mutations), ["DELETE", "PATCH", "POST"])
        for cmd, stdin in mutations:
            self.assertNotIn(TOKEN, " ".join(cmd))
            if stdin:
                body = json.loads(stdin)
                self.assertEqual(body["events"], ["push"])
                self.assertTrue(body["config"]["url"].startswith(API + "/api/v1/teams/dev/"))
                self.assertTrue(body["config"]["url"].endswith("webhook_token=" + TOKEN))
        self.assertNotIn(TOKEN, reconciler.out.getvalue())

    def test_rejected_token_or_declined_confirmation_changes_nothing(self):
        def make_runner():
            return FakeRunner({"app": [resource("repo", "git@github.com:org/app.git")]},
                              {"org/app": repo("org/app")}, {})
        for rejected, answer in ((True, "apply"), (False, "no")):
            runner = make_runner()
            with patch.object(gw, "run", runner), \
                    patch.object(gw.Reconciler, "check_token", return_value=not rejected), \
                    patch.dict("os.environ", {"CONCOURSE_WEBHOOK_TOKEN": TOKEN}), \
                    patch("builtins.input", return_value=answer), \
                    patch("sys.stdout", io.StringIO()) as out:
                with self.assertRaises(SystemExit):
                    gw.main(["apply", "-t", "ci"])
            self.assertEqual(runner.mutations(), [])
            self.assertNotIn(TOKEN, out.getvalue())

    def test_apply_with_confirmation_bypass_mutates_after_token_check(self):
        runner = FakeRunner({"app": [resource("repo", "git@github.com:org/app.git")]},
                            {"org/app": repo("org/app")}, {})
        with patch.object(gw, "run", runner), \
                patch.object(gw.Reconciler, "check_token", return_value=True) as check, \
                patch.object(gw.getpass, "getpass", return_value=TOKEN), \
                patch.dict("os.environ", {}, clear=True), \
                patch("builtins.input", side_effect=AssertionError("prompted")), \
                patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(gw.main(["apply", "-t", "ci", "--yes"]), 0)
        check.assert_called_once_with("/api/v1/teams/dev/pipelines/app/resources/repo/check/webhook", TOKEN)
        self.assertEqual([cmd[3] for cmd, _ in runner.mutations()], ["POST"])
        self.assertIn("done    create org/app", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())

    def test_apply_without_token_or_changes(self):
        empty = FakeRunner({"app": [resource("repo", "git@github.com:org/app.git")]},
                           {"org/app": repo("org/app")},
                           {"org/app": [hook(1, check_url(API, "dev", "app", "repo", TOKEN))]})
        with patch.object(gw, "run", empty), patch("sys.stdout", io.StringIO()), \
                patch.dict("os.environ", {"CONCOURSE_WEBHOOK_TOKEN": TOKEN}), \
                patch.object(gw.Reconciler, "check_token", side_effect=AssertionError("probed")):
            self.assertEqual(gw.main(["apply", "-t", "ci"]), 0)
        self.assertEqual(empty.mutations(), [])
        with patch.object(gw, "run", empty), patch("sys.stdout", io.StringIO()), \
                patch.dict("os.environ", {}, clear=True), patch.object(gw.getpass, "getpass", return_value=""):
            with self.assertRaisesRegex(SystemExit, "no token"):
                gw.main(["apply", "-t", "ci"])

    def test_deletions_alone_skip_the_token_probe(self):
        runner = FakeRunner({"app": [resource("repo", "git@github.com:org/app.git")]},
                            {"org/app": repo("org/app")},
                            {"org/app": [hook(1, check_url(API, "dev", "app", "repo", TOKEN)),
                                         hook(2, check_url(OLD, "dev", "app", "repo", TOKEN))]})
        with patch.object(gw, "run", runner), patch("sys.stdout", io.StringIO()), \
                patch.dict("os.environ", {"CONCOURSE_WEBHOOK_TOKEN": TOKEN}), \
                patch.object(gw.Reconciler, "check_token", side_effect=AssertionError("probed")):
            self.assertEqual(gw.main(["apply", "-t", "ci", "--prune", "--prune-host", OLD, "--yes"]), 0)
        self.assertEqual([cmd[3] for cmd, _ in runner.mutations()], ["DELETE"])

    def test_unknown_target_and_instanced_pipelines(self):
        runner = FakeRunner({}, {}, {}, extra_pipelines=[{"name": "inst", "instance_vars": {"a": 1}},
                                                         {"name": "old", "archived": True}])
        with self.assertRaisesRegex(SystemExit, "not found"):
            gw.Reconciler("missing", runner=runner, out=io.StringIO())
        reconciler = self.reconciler(runner)
        actions, blocked = reconciler.plan()
        self.assertEqual(blocked, ["inst: instanced pipeline not inspected; Concourse webhooks cannot target instances"])
        reconciler.show(actions, blocked)
        self.assertEqual(reconciler.out.getvalue().splitlines(),
                         ["BLOCKED " + blocked[0], "0 change(s), 1 blocked"])

    def test_script_runs_standalone(self):
        result = subprocess.run([sys.executable, str(ROOT / "ci/github-webhooks.py"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--prune-host", result.stdout)

    def test_verify_reports_latest_delivery_of_managed_hooks(self):
        runner = FakeRunner(
            {"app": [resource("repo", "git@github.com:org/app.git"),
                     resource("deps", "git@github.com:org/app.git"),
                     resource("x", "git@github.com:org/archived.git"),
                     resource("y", "git@github.com:org/missing.git")]},
            {"org/app": repo("org/app"), "org/archived": repo("org/archived", archived=True)},
            {"org/app": [hook(1, check_url(API, "dev", "app", "repo")),
                         hook(2, check_url(API, "dev", "app", "deps")),
                         hook(3, check_url(API, "dev", "app", "gone")),
                         hook(4, check_url(API, "main", "app", "repo")),
                         hook(5, check_url(OLD, "dev", "app", "repo")),
                         hook(6, "https://hooks.slack.com/services/x")]},
            deliveries={1: 201, 2: 500})
        reconciler = self.reconciler(runner, prune_hosts=[OLD])
        self.assertEqual(reconciler.verify(), 2)
        lines = reconciler.out.getvalue().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["ok", "FAIL", "FAIL"])
        self.assertIn("last_delivery=None", lines[2])
        with patch.object(gw, "run", runner), patch("sys.stdout", io.StringIO()):
            self.assertEqual(gw.main(["verify", "-t", "ci"]), 1)
        healthy = FakeRunner({"app": [resource("repo", "git@github.com:org/app.git")]},
                             {"org/app": repo("org/app")},
                             {"org/app": [hook(1, check_url(API, "dev", "app", "repo"))]}, deliveries={1: 200})
        with patch.object(gw, "run", healthy), patch("sys.stdout", io.StringIO()):
            self.assertEqual(gw.main(["verify", "-t", "ci"]), 0)

    def test_check_token_accepts_only_success(self):
        reconciler = self.reconciler(FakeRunner({}, {}, {}))
        response = unittest.mock.MagicMock(status=201)
        response.__enter__.return_value = response
        with patch.object(gw.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(reconciler.check_token("/p", TOKEN))
        request = urlopen.call_args.args[0]
        self.assertEqual((request.method, request.full_url), ("POST", f"{API}/p?webhook_token={TOKEN}"))
        error = gw.urllib.error.HTTPError(API, 401, "Unauthorized", {}, None)
        with patch.object(gw.urllib.request, "urlopen", side_effect=error):
            self.assertFalse(reconciler.check_token("/p", TOKEN))

    def test_run_returns_stdout_and_raises_on_failure(self):
        self.assertEqual(gw.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], stdin="hi"), "hi\n")
        with self.assertRaises(subprocess.CalledProcessError):
            gw.run([sys.executable, "-c", "raise SystemExit(3)"])

    def test_plan_never_asks_for_a_token(self):
        runner = FakeRunner({"app": [resource("repo", "git@github.com:org/app.git")]},
                            {"org/app": repo("org/app")}, {})
        with patch.object(gw, "run", runner), patch("sys.stdout", io.StringIO()) as out, \
                patch.object(gw.getpass, "getpass", side_effect=AssertionError("prompted")):
            self.assertEqual(gw.main(["plan", "-t", "ci"]), 0)
        self.assertIn("create", out.getvalue())
        self.assertEqual(runner.mutations(), [])


if __name__ == "__main__":
    unittest.main()
