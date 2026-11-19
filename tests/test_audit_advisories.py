"""Run shared/ci/tasks/nodejs-audit-advisories.js against a stub bulk advisory endpoint."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "shared/ci/tasks/nodejs-audit-advisories.js"
WRAPPER = ROOT / "shared/ci/tasks/nodejs-audit.sh"

PROD = {
    "@scope/app-dep": ["1.0.0"],
    "opt-dep": ["1.5.0"],
    "other-peer": ["1.0.0"],
    "opt-transitive": ["1.0.0"],
    "peer-dep": ["2.0.0"],
    "real-pkg": ["3.1.0"],
    "transitive": ["2.0.0"],
    "ws-dep": ["4.0.0"],
}
ALL = {**PROD, "dev-only": ["1.0.0"], "dev-transitive": ["1.0.0"]}

PNPM_V9 = textwrap.dedent("""\
    lockfileVersion: '9.0'

    settings:
      autoInstallPeers: true
      excludeLinksFromLockfile: false

    overrides:
      transitive: 2.0.0

    importers:

      .:
        dependencies:
          '@scope/app-dep':
            specifier: ^1.0.0
            version: 1.0.0(other-peer@1.0.0)(peer-dep@2.0.0)
          aliased:
            specifier: npm:real-pkg@^3.0.0
            version: real-pkg@3.1.0
          local-ws:
            specifier: workspace:*
            version: link:packages/ws
        optionalDependencies:
          opt-dep:
            specifier: ^1.0.0
            version: 1.5.0
        devDependencies:
          dev-only:
            specifier: ^1.0.0
            version: 1.0.0

      packages/ws:
        dependencies:
          ws-dep:
            specifier: ^4.0.0
            version: 4.0.0

    packages:

      '@scope/app-dep@1.0.0':
        resolution: {integrity: sha512-a}
        peerDependencies:
          peer-dep: ^2.0.0

      dev-only@1.0.0:
        resolution: {integrity: sha512-b}
        deprecated: 'use something else: it is: better'

      peer-dep@2.0.0:
        resolution: {integrity: sha512-c}
        cpu: [x64]
        os:
          - linux

    snapshots:

      '@scope/app-dep@1.0.0(other-peer@1.0.0)(peer-dep@2.0.0)':
        dependencies:
          other-peer: 1.0.0
          peer-dep: 2.0.0
          transitive: 2.0.0

      dev-only@1.0.0:
        dependencies:
          dev-transitive: 1.0.0

      dev-transitive@1.0.0: {}

      opt-dep@1.5.0:
        optionalDependencies:
          opt-transitive: 1.0.0

      opt-transitive@1.0.0: {}

      other-peer@1.0.0: {}

      peer-dep@2.0.0: {}

      real-pkg@3.1.0: {}

      transitive@2.0.0:
        dependencies:
          ws-dep: 4.0.0

      ws-dep@4.0.0:
        dependencies:
          transitive: 2.0.0
    """)

PNPM_V6 = textwrap.dedent("""\
    lockfileVersion: '6.0'

    settings:
      autoInstallPeers: true

    importers:

      .:
        dependencies:
          '@scope/app-dep':
            specifier: ^1.0.0
            version: 1.0.0(other-peer@1.0.0)(peer-dep@2.0.0)
          aliased:
            specifier: npm:real-pkg@^3.0.0
            version: /real-pkg@3.1.0
          local-ws:
            specifier: workspace:*
            version: link:packages/ws
        optionalDependencies:
          opt-dep:
            specifier: ^1.0.0
            version: 1.5.0
        devDependencies:
          dev-only:
            specifier: ^1.0.0
            version: 1.0.0

      packages/ws:
        dependencies:
          ws-dep:
            specifier: ^4.0.0
            version: 4.0.0

    packages:

      /@scope/app-dep@1.0.0(other-peer@1.0.0)(peer-dep@2.0.0):
        resolution: {integrity: sha512-a}
        peerDependencies:
          other-peer: ^1.0.0
          peer-dep: ^2.0.0
        dependencies:
          other-peer: 1.0.0
          peer-dep: 2.0.0
          transitive: 2.0.0
        dev: false

      /dev-only@1.0.0:
        resolution: {integrity: sha512-b}
        dependencies:
          dev-transitive: 1.0.0
        dev: true

      /dev-transitive@1.0.0:
        resolution: {integrity: sha512-c}
        dev: true

      /opt-dep@1.5.0:
        resolution: {integrity: sha512-d}
        optionalDependencies:
          opt-transitive: 1.0.0
        dev: false
        optional: true

      /opt-transitive@1.0.0:
        resolution: {integrity: sha512-e}
        dev: false
        optional: true

      /other-peer@1.0.0:
        resolution: {integrity: sha512-j}
        dev: false

      /peer-dep@2.0.0:
        resolution: {integrity: sha512-f}
        dev: false

      /real-pkg@3.1.0:
        resolution: {integrity: sha512-g}
        dev: false

      /transitive@2.0.0:
        resolution: {integrity: sha512-h}
        dependencies:
          ws-dep: 4.0.0
        dev: false

      /ws-dep@4.0.0:
        resolution: {integrity: sha512-i}
        dependencies:
          transitive: 2.0.0
        dev: false
    """)

PNPM_V6_SINGLE = textwrap.dedent("""\
    lockfileVersion: '6.0'

    dependencies:
      prod-dep:
        specifier: ^1.0.0
        version: 1.0.0

    devDependencies:
      dev-only:
        specifier: ^1.0.0
        version: 1.0.0

    packages:

      /dev-only@1.0.0:
        resolution: {integrity: sha512-a}
        dev: true

      /prod-dep@1.0.0:
        resolution: {integrity: sha512-b}
        dev: false
    """)

YARN_V1 = textwrap.dedent("""\
    # THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY.
    # yarn lockfile v1


    "@scope/app-dep@^1.0.0":
      version "1.0.0"
      resolved "https://registry.yarnpkg.com/@scope/app-dep/-/app-dep-1.0.0.tgz"
      integrity sha512-a
      dependencies:
        "@scope/inner" "^1.0.0"
        transitive "^2.0.0"
      optionalDependencies:
        opt-transitive "1.0.0"

    "@scope/inner@^1.0.0", "@scope/inner@^1.0.1":
      version "1.0.1"

    "aliased@npm:real-pkg@^3.0.0":
      version "3.1.0"

    dev-only@^1.0.0:
      version "1.0.0"
      dependencies:
        dev-transitive "1.0.0"

    dev-transitive@1.0.0:
      version "1.0.0"

    opt-transitive@1.0.0:
      version "1.0.0"

    transitive@^2.0.0, transitive@^2.0.0-rc.1:
      version "2.0.0"
      dependencies:
        "@scope/app-dep" "^1.0.0"

    ws-dep@4.0.0:
      version "4.0.0"
    """)

YARN_MANIFESTS = {
    "package.json": {
        "name": "root",
        "private": True,
        "workspaces": ["packages/*"],
        "dependencies": {"@scope/app-dep": "^1.0.0", "aliased": "npm:real-pkg@^3.0.0"},
        "devDependencies": {"dev-only": "^1.0.0"},
    },
    "packages/ws/package.json": {"name": "ws", "dependencies": {"ws-dep": "4.0.0"}},
    "packages/other/package.json": {"name": "other", "dependencies": {"ws": "*"}},
}
YARN_PROD = {
    "@scope/app-dep": ["1.0.0"],
    "@scope/inner": ["1.0.1"],
    "opt-transitive": ["1.0.0"],
    "real-pkg": ["3.1.0"],
    "transitive": ["2.0.0"],
    "ws-dep": ["4.0.0"],
}
YARN_ALL = {**YARN_PROD, "dev-only": ["1.0.0"], "dev-transitive": ["1.0.0"]}


def advisory(severity, title="bad"):
    return {
        "id": 1,
        "url": f"https://github.com/advisories/GHSA-{severity[:4]}-0000-0000",
        "title": title,
        "severity": severity,
        "vulnerable_versions": "<9.9.9",
    }


class StubRegistry:
    """Answers each POST with the next scripted response, then with `default`."""

    def __init__(self):
        self.requests = []
        self.responses = []
        self.default: tuple = (200, {}, False)
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["content-length"])
                registry.requests.append(json.loads(self.rfile.read(length)))
                status, body, gzipped = (
                    registry.responses.pop(0) if registry.responses else registry.default
                )
                if callable(body):
                    body = body(registry.requests[-1])
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                if gzipped:
                    data = gzip.compress(data)
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/bulk"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AuditAdvisoriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.registry = StubRegistry()
        self.addCleanup(self.registry.close)

    def write(self, files):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content))

    def run_audit(self, *args, url=None):
        env = dict(
            os.environ,
            AUDIT_ADVISORIES_URL=url or self.registry.url,
            AUDIT_RETRY_DELAY_MS="0",
        )
        return subprocess.run(
            ["node", str(SCRIPT), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def assert_exit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def requested(self):
        merged = {}
        for request in self.registry.requests:
            merged.update(request)
        return merged

    def test_pnpm_v9_audits_the_production_graph(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        result = self.run_audit()
        self.assert_exit(result, 0)
        self.assertEqual(self.requested(), PROD)
        self.assertIn("auditing 8 production package versions from pnpm-lock.yaml", result.stderr)

    def test_pnpm_v6_audits_the_production_graph(self):
        self.write({"pnpm-lock.yaml": PNPM_V6})
        self.assert_exit(self.run_audit(), 0)
        self.assertEqual(self.requested(), PROD)

    def test_pnpm_v6_single_project_lockfile(self):
        self.write({"pnpm-lock.yaml": PNPM_V6_SINGLE})
        self.assert_exit(self.run_audit(), 0)
        self.assertEqual(self.requested(), {"prod-dep": ["1.0.0"]})

    def test_pnpm_env_lockfile_document_is_read(self):
        env_document = "lockfileVersion: '9.0'\n\nimporters:\n\n  .:\n    configDependencies: {}\n"
        self.write({"pnpm-lock.yaml": f"---\n{env_document}\n---\n{PNPM_V9}"})
        self.assert_exit(self.run_audit(), 0)
        self.assertEqual(self.requested(), PROD)

    def test_yarn_v1_audits_the_production_graph(self):
        self.write({"yarn.lock": YARN_V1, **YARN_MANIFESTS})
        result = self.run_audit()
        self.assert_exit(result, 0)
        self.assertEqual(self.requested(), YARN_PROD)
        self.assertIn("production package versions from yarn.lock", result.stderr)

    def test_yarn_workspace_pattern_forms(self):
        root = YARN_MANIFESTS["package.json"]
        for workspaces in (["packages/ws", "packages/other"], {"packages": ["packages/*"]}):
            with self.subTest(workspaces=workspaces):
                self.registry.requests.clear()
                self.write({"yarn.lock": YARN_V1, **YARN_MANIFESTS,
                            "package.json": {**root, "workspaces": workspaces}})
                result = self.run_audit()
                self.assert_exit(result, 0)
                self.assertNotIn("auditing every package", result.stderr)
                self.assertEqual(self.requested(), YARN_PROD)

    def test_unresolved_graph_audits_every_lockfile_package(self):
        broken_pnpm = PNPM_V9.replace("version: 4.0.0", "version: 4.0.1")
        manifests = dict(YARN_MANIFESTS)
        manifests["packages/ws/package.json"] = {"name": "ws", "dependencies": {"gone": "^1"}}
        manifests["unlisted/package.json"] = {"name": "unlisted"}
        for files, expected in (
            ({"pnpm-lock.yaml": broken_pnpm}, ALL),
            ({"yarn.lock": YARN_V1, **manifests}, YARN_ALL),
            ({"yarn.lock": YARN_V1}, YARN_ALL),  # no package.json
            (
                {
                    "yarn.lock": YARN_V1,
                    **YARN_MANIFESTS,
                    "package.json": {
                        **YARN_MANIFESTS["package.json"],
                        "workspaces": ["packages/ws", "packages/missing"],
                    },
                },
                YARN_ALL,
            ),
            (
                {
                    "yarn.lock": YARN_V1,
                    **YARN_MANIFESTS,
                    "package.json": {**YARN_MANIFESTS["package.json"], "workspaces": ["pkgs/**"]},
                },
                YARN_ALL,
            ),
        ):
            with self.subTest(files=sorted(files)):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.registry.requests.clear()
                self.write(files)
                result = self.run_audit()
                self.assert_exit(result, 0)
                self.assertIn("auditing every package in", result.stderr)
                self.assertEqual(self.requested(), expected)

    def test_severity_threshold(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        self.registry.default = (
            200,
            {"transitive": [advisory("high", "proto pollution")], "real-pkg": [advisory("moderate")]},
            False,
        )
        for level, code, listed, unlisted in (
            ("critical", 0, [], ["transitive", "real-pkg"]),
            ("high", 1, ["high: transitive"], ["real-pkg"]),
            ("moderate", 1, ["high: transitive", "moderate: real-pkg"], []),
        ):
            with self.subTest(level=level):
                result = self.run_audit("--level", level)
                self.assert_exit(result, code)
                for line in listed:
                    self.assertIn(line, result.stdout)
                for name in unlisted:
                    self.assertNotIn(name, result.stdout)
        result = self.run_audit()
        self.assert_exit(result, 1)
        self.assertIn("title: proto pollution", result.stdout)
        self.assertIn("installed versions: 2.0.0", result.stdout)

    def test_gzip_body_without_content_encoding(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        self.registry.default = (200, {"ws-dep": [advisory("critical")]}, True)
        result = self.run_audit()
        self.assert_exit(result, 1)
        self.assertIn("critical: ws-dep", result.stdout)

    def test_registry_failures_fail_closed(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        for responses, requests, reason in (
            ([(410, b"Gone", False)], 1, "HTTP 410: Gone"),
            ([(400, b"bad request", False)], 1, "HTTP 400"),
            ([(200, b"<html>not json</html>", False)], 1, "unreadable advisory response"),
            ([(200, b"\x1f\x8bnot gzip", False)], 1, "unreadable advisory response"),
            ([(200, [], False)], 1, "not a JSON object"),
            ([(200, {"ws-dep": {"severity": "high"}}, False)], 1, "ws-dep are not a list"),
            ([(200, {"ws-dep": [{"severity": "severe"}]}, False)], 1, "unknown severity 'severe'"),
            ([(503, b"down", False)] * 3, 3, "HTTP 503: down"),
            ([(429, b"slow down", False)] * 3, 3, "HTTP 429"),
        ):
            with self.subTest(reason=reason):
                self.registry.requests.clear()
                self.registry.responses = list(responses)
                result = self.run_audit()
                self.assert_exit(result, 2)
                self.assertIn("audit could not run", result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertEqual(len(self.registry.requests), requests)
        closed = StubRegistry()
        closed.close()
        result = self.run_audit(url=closed.url)
        self.assert_exit(result, 2)
        self.assertIn("advisory request failed", result.stderr)

    def test_transient_registry_errors_are_retried(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        self.registry.responses = [(503, b"down", False), (200, {"ws-dep": [advisory("high")]}, False)]
        result = self.run_audit()
        self.assert_exit(result, 1)
        self.assertEqual(len(self.registry.requests), 2)
        self.assertIn("high: ws-dep", result.stdout)

    def test_large_package_sets_are_chunked(self):
        names = [f"pkg-{i:04d}" for i in range(1500)]
        lock = "# yarn lockfile v1\n\n" + "".join(f'{n}@1.0.0:\n  version "1.0.0"\n\n' for n in names)
        manifest = {"name": "root", "dependencies": {n: "1.0.0" for n in names}}
        self.write({"yarn.lock": lock, "package.json": manifest})
        last = names[-1]
        self.registry.default = (
            200,
            lambda request: {last: [advisory("high")]} if last in request else {},
            False,
        )
        result = self.run_audit()
        self.assert_exit(result, 1)
        self.assertEqual([len(r) for r in self.registry.requests], [1000, 500])
        self.assertIn(f"high: {last}", result.stdout)

    def test_long_findings_output_is_not_truncated(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        many = [advisory("high", f"finding {i}") for i in range(3000)]
        self.registry.default = (200, {"ws-dep": many}, False)
        result = self.run_audit()
        self.assert_exit(result, 1)
        self.assertEqual(result.stdout.count("title: finding "), 3000)

    def test_unsupported_input_fails(self):
        for files, message in (
            ({}, "no yarn.lock or pnpm-lock.yaml found"),
            ({"pnpm-lock.yaml": "lockfileVersion: 5.4\n\npackages:\n\n  /a/1.0.0:\n    dev: false\n"},
             "unsupported pnpm lockfileVersion"),
            ({"pnpm-lock.yaml": "lockfileVersion: '9.0'\n\nimporters:\n\n  .: {}\n"},
             "no packages parsed"),
            ({"pnpm-lock.yaml": PNPM_V9 + "this is not yaml\n"}, "cannot parse lockfile line"),
            ({"yarn.lock": '__metadata:\n  version: 8\n\n"a@npm:1.0.0":\n  version: 1.0.0\n'},
             "only yarn classic v1"),
        ):
            with self.subTest(message=message):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.write(files)
                result = self.run_audit()
                self.assert_exit(result, 2)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.registry.requests, [])

    def test_invalid_level_fails(self):
        self.write({"pnpm-lock.yaml": PNPM_V9})
        for args in (["--level", "severe"], ["--level"], ["--prod"]):
            with self.subTest(args=args):
                self.assert_exit(self.run_audit(*args), 2)
        self.assert_exit(self.run_audit("--level=critical"), 0)

    def test_yarn_lock_wins_when_both_lockfiles_exist(self):
        self.write({"yarn.lock": YARN_V1, "pnpm-lock.yaml": PNPM_V9, **YARN_MANIFESTS})
        result = self.run_audit()
        self.assert_exit(result, 0)
        self.assertIn("from yarn.lock", result.stderr)
        self.assertEqual(self.requested(), YARN_PROD)

    def test_concourse_wrapper_passes_level_and_repo_root(self):
        # Concourse runs the vendored task by a path relative to the build dir
        tasks = self.tmp / "pipeline-tasks/ci/vendor/tasks"
        tasks.mkdir(parents=True)
        shutil.copy(WRAPPER, tasks)
        shutil.copy(SCRIPT, tasks)
        self.write({"pnpm-lock.yaml": PNPM_V9})
        self.registry.default = (200, {"ws-dep": [advisory("high")]}, False)
        for level, code in ((None, 1), ("high", 1), ("critical", 0)):
            with self.subTest(level=level):
                env = dict(os.environ, REPO_ROOT="repo", AUDIT_ADVISORIES_URL=self.registry.url)
                env.pop("LEVEL", None)
                if level:
                    env["LEVEL"] = level
                result = subprocess.run(
                    ["pipeline-tasks/ci/vendor/tasks/nodejs-audit.sh"],
                    cwd=self.tmp,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assert_exit(result, code)
                self.assertIn(f"at level '{level or 'high'}'", result.stderr)


if __name__ == "__main__":
    unittest.main()
