"""Read authenticated GCR manifests; never publish or promote an image."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

CA_SHA256 = '81ffe9877138a9e9bee56e067479a5abd59c65cdbb310d830b13108e1afe4e33'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
ACCEPT = 'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.index.v1+json'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Registry authentication redirects are forbidden')


def checked_digest(value):
    if not DIGEST.fullmatch(value):
        raise RuntimeError('Invalid image digest')
    return value


class Registry:
    def __init__(self, repository):
        host, separator, path = repository.partition('/')
        if host not in ('gcr.io', 'us.gcr.io', 'eu.gcr.io', 'asia.gcr.io') or not separator or not re.fullmatch(r'[a-z0-9][a-z0-9/_-]+', path):
            raise RuntimeError('Only the configured GCR repositories are supported')
        self.host, self.repository = host, path
        bundle = Path(__file__).with_name('registry-ca.pem')
        if hashlib.sha256(bundle.read_bytes()).hexdigest() != CA_SHA256:
            raise RuntimeError('Registry verifier CA content drift')
        context = ssl.create_default_context(cafile=str(bundle))
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        username, password = os.environ['REGISTRY_USERNAME'], os.environ['REGISTRY_PASSWORD']
        if bool(username) != bool(password):
            raise RuntimeError('Incomplete registry credentials')
        credential = username + ':' + password
        self.authorization = 'Basic ' + base64.b64encode(credential.encode()).decode() if username else None

    def get(self, url, *, token=False):
        headers = {'Accept': ACCEPT}
        if self.authorization:
            headers['Authorization'] = self.authorization
        request = urllib.request.Request(url, headers=headers)
        try:
            return self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code != 401 or token:
                raise
            challenge = error.headers.get('WWW-Authenticate', '')
            if not challenge.lower().startswith('bearer '):
                raise RuntimeError('Unsupported registry authentication') from None
            fields = dict(re.findall(r'(realm|service|scope)="([^"]+)"', challenge))
            realm = urllib.parse.urlsplit(fields['realm'])
            if realm.scheme != 'https' or realm.netloc != self.host or realm.query or realm.fragment:
                raise RuntimeError('Untrusted registry token realm')
            query = urllib.parse.urlencode({key: fields[key] for key in ('service', 'scope') if key in fields})
            with self.get(fields['realm'] + '?' + query, token=True) as response:
                payload = json.load(response)
            self.authorization = 'Bearer ' + payload['token']
            return self.get(url, token=True)

    def digest(self, reference, *, missing_ok=False):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', reference):
            raise RuntimeError('Invalid registry reference')
        url = f'https://{self.host}/v2/{self.repository}/manifests/{reference}'
        try:
            with self.get(url) as response:
                body = response.read()
                digest = checked_digest(response.headers['Docker-Content-Digest'])
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise
        if 'sha256:' + hashlib.sha256(body).hexdigest() != digest:
            raise RuntimeError('Registry manifest digest disagrees with its bytes')
        return digest


def snapshot(registry, candidate, destination):
    receipt = {'previous_candidate_digest': registry.digest(candidate, missing_ok=True)}
    destination.write_text(json.dumps(receipt) + '\n')
    print(json.dumps(receipt), flush=True)


def verify(registry, candidate, destination, build_file, put_file, previous_file):
    built = checked_digest(build_file.read_text().strip())
    published = checked_digest(put_file.read_text().strip())
    candidate_digest = registry.digest(candidate)
    content_tag = 'sha256-' + built.removeprefix('sha256:')
    content_digest = registry.digest(content_tag)
    if len({built, published, candidate_digest, content_digest}) != 1:
        raise RuntimeError('Build, publication and registry tags do not identify the same manifest')
    receipt = json.loads(previous_file.read_text())
    receipt.update(build_digest=built, put_digest=published, candidate_digest=candidate_digest, content_tag_digest=content_digest)
    destination.write_text(json.dumps(receipt, sort_keys=True) + '\n')
    print(json.dumps(receipt, sort_keys=True), flush=True)


def main(args):
    mode, repository, candidate, output, *files = args
    registry = Registry(repository)
    if mode == 'snapshot' and not files:
        snapshot(registry, candidate, Path(output))
    elif mode == 'verify' and len(files) == 3:
        verify(registry, candidate, Path(output), *map(Path, files))
    else:
        raise RuntimeError('Invalid registry proof invocation')


if __name__ == '__main__':
    main(sys.argv[1:])
