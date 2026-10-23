import hashlib
import importlib.util
import io
import json
import os
import runpy
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'images/trust/registry-proof.py'
spec = importlib.util.spec_from_file_location('registry_proof', MODULE_PATH)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
A = 'sha256:' + 'a' * 64
B = 'sha256:' + 'b' * 64


def response(body=b'{}', headers=None):
    result = io.BytesIO(body)
    result.headers = headers or {}
    return result


def failure(code, challenge=''):
    return urllib.error.HTTPError('https://us.gcr.io/v2/test', code, 'test', {'WWW-Authenticate': challenge}, None)


class RegistryProofTests(unittest.TestCase):
    def registry(self):
        with patch.dict(os.environ, REGISTRY_USERNAME='test', REGISTRY_PASSWORD='secret'):
            return proof.Registry('us.gcr.io/project/image')

    def test_cli_entrypoint(self):
        with patch.object(sys, "argv", [str(MODULE_PATH), "snapshot", "untrusted/a", "candidate", "output"]), self.assertRaises(RuntimeError):
            runpy.run_path(str(MODULE_PATH), run_name="__main__")

    def test_init_and_no_redirect(self):
        registry = self.registry()
        with patch.dict(os.environ, REGISTRY_USERNAME='', REGISTRY_PASSWORD=''):
            anonymous = proof.Registry('us.gcr.io/project/image')
            self.assertIsNone(anonymous.authorization)
            anonymous.opener = Mock()
            anonymous.get('https://us.gcr.io/v2/test')
            self.assertNotIn('Authorization', anonymous.opener.open.call_args.args[0].headers)
        for username, password in (('user', ''), ('', 'password')):
            with patch.dict(os.environ, REGISTRY_USERNAME=username, REGISTRY_PASSWORD=password), self.assertRaises(RuntimeError):
                proof.Registry('us.gcr.io/project/image')
        self.assertEqual(registry.host, 'us.gcr.io')
        for repository in ('example.com/a', 'us.gcr.io', 'us.gcr.io/x/../../secret'):
            with self.assertRaises(RuntimeError): proof.Registry(repository)
        with patch.object(proof.Path, 'read_bytes', return_value=b'changed'), self.assertRaises(RuntimeError):
            proof.Registry('us.gcr.io/project/image')
        with self.assertRaises(RuntimeError): proof.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other')
        with self.assertRaises(RuntimeError): proof.checked_digest('sha256:short')

    def test_manifest_identity_missing_and_failures(self):
        registry = self.registry()
        body = b'{"schemaVersion":2}'
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        registry.get = Mock(return_value=response(body, {'Docker-Content-Digest': digest}))
        self.assertEqual(registry.digest('candidate'), digest)
        registry.get = Mock(return_value=response(body, {'Docker-Content-Digest': A}))
        with self.assertRaises(RuntimeError): registry.digest('candidate')
        for code in (401, 403, 404, 500):
            registry.get = Mock(side_effect=failure(code))
            if code == 404: self.assertIsNone(registry.digest('candidate', missing_ok=True))
            else:
                with self.assertRaises(urllib.error.HTTPError): registry.digest('candidate', missing_ok=True)
            with self.assertRaises(urllib.error.HTTPError): registry.digest('candidate')
        with self.assertRaises(RuntimeError): registry.digest('../bad')

    def test_authentication_success_and_fail_closed(self):
        registry = self.registry()
        registry.opener = Mock()
        final = response()
        registry.opener.open.return_value = final
        self.assertIs(registry.get('https://us.gcr.io/v2/x'), final)
        challenge = 'Bearer realm="https://us.gcr.io/v2/token",service="us.gcr.io",scope="repository:project/image:pull"'
        registry.opener.open.side_effect = [failure(401, challenge), response(b'{"token":"test-token"}'), final]
        self.assertIs(registry.get('https://us.gcr.io/v2/x'), final)
        self.assertEqual(registry.authorization, 'Bearer test-token')
        for code, token in ((500, False), (401, True)):
            registry.opener.open.side_effect = failure(code)
            with self.assertRaises(urllib.error.HTTPError): registry.get('https://us.gcr.io/v2/x', token=token)
        for challenge in ('Basic realm="x"', 'Bearer realm="http://us.gcr.io/token"', 'Bearer realm="https://other/token"', 'Bearer realm="https://us.gcr.io/token?bad=1"', 'Bearer realm="https://us.gcr.io/token#fragment"'):
            registry.opener.open.side_effect = failure(401, challenge)
            with self.assertRaises(RuntimeError): registry.get('https://us.gcr.io/v2/x')
        # Optional challenge fields need not be present.
        registry.opener.open.side_effect = [failure(401, 'Bearer realm="https://us.gcr.io/token"'), response(b'{"token":"test-token"}'), final]
        self.assertIs(registry.get('https://us.gcr.io/v2/x'), final)

    def test_snapshot_readback_and_all_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build, put, previous, output = [root / name for name in ('build', 'put', 'previous', 'output')]
            registry = Mock()
            registry.digest.return_value = B
            proof.snapshot(registry, 'candidate', previous)
            self.assertEqual(json.loads(previous.read_text())['previous_candidate_digest'], B)
            registry.digest.return_value = None
            proof.snapshot(registry, 'candidate', previous)
            for values in ((A,A,A,A), (B,A,A,A), (A,B,A,A), (A,A,B,A), (A,A,A,B)):
                build.write_text(values[0]);put.write_text(values[1]);registry.digest.side_effect=values[2:]
                if len(set(values)) == 1:
                    proof.verify(registry, 'candidate', output, build, put, previous)
                    self.assertEqual(json.loads(output.read_text())['content_tag_digest'], A)
                else:
                    with self.assertRaises(RuntimeError): proof.verify(registry, 'candidate', output, build, put, previous)
            with patch.object(proof, 'Registry', return_value=registry), patch.object(proof, 'snapshot') as snapshot, patch.object(proof, 'verify') as verify:
                proof.main(['snapshot', 'repo', 'candidate', str(output)])
                snapshot.assert_called_once()
                proof.main(['verify', 'repo', 'candidate', str(output), str(build), str(put), str(previous)])
                verify.assert_called_once()
                for args in (['bad', 'repo', 'candidate', str(output)], ['snapshot', 'repo', 'candidate', str(output), 'extra'], ['verify', 'repo', 'candidate', str(output)]):
                    with self.assertRaises(RuntimeError): proof.main(args)


if __name__ == '__main__':
    unittest.main()
