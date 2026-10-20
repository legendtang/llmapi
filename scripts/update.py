"""Resolve upstream digests, then use the official flyctl deploy workflow.

Only Python's standard library is needed. No application credentials are read.
The Fly token is scoped to this app and provided through GitHub Secrets.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP = os.environ.get('FLY_APP', 'llmapi')
ROOT = Path('.deploy')
ACCEPT = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])


def fetch(url, headers=None, body=None):
    request = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(),
        headers=headers or {},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response), response.headers


def api(path):
    token = os.environ['FLY_API_TOKEN']
    return fetch('https://api.machines.dev/v1/apps/' + APP + '/' + path,
                 {'Authorization': 'Bearer ' + token})[0]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def summary(text):
    print(text, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(text + '\n\n')


def output(key, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(key + '=' + str(value) + '\n')


def resolve(repository):
    query = urllib.parse.urlencode({
        'service': 'registry.docker.io', 'scope': 'repository:' + repository + ':pull',
    })
    token = fetch('https://auth.docker.io/token?' + query)[0]['token']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': ACCEPT}
    manifest, response = fetch(
        'https://registry-1.docker.io/v2/' + repository + '/manifests/latest', headers)
    index_digest = response['Docker-Content-Digest']
    digest = index_digest
    if 'manifests' in manifest:
        candidates = [entry for entry in manifest['manifests']
                      if entry.get('platform', {}).get('os') == 'linux'
                      and entry.get('platform', {}).get('architecture') == 'amd64']
        require(len(candidates) == 1, 'Expected one linux/amd64 image for ' + repository)
        digest = candidates[0]['digest']
    require(digest.startswith('sha256:'), 'Unexpected image digest')
    return repository + '@' + digest, {index_digest, digest}


def prepare():
    machines = api('machines')
    require(len(machines) == 1, 'Expected exactly one existing Machine; refusing to create resources')
    live = api('machines/' + machines[0]['id'])
    require(live['region'] == 'nrt', 'Expected Tokyo region nrt')
    require(live['state'] == 'started', 'Existing Machine must be running')
    config = live['config']
    require(config['guest'] == {'cpu_kind': 'shared', 'cpus': 1, 'memory_mb': 256},
            'Resource settings changed; review the deployment configuration first')
    require(len(config['mounts']) == 1 and config['mounts'][0]['path'] == '/home'
            and config['mounts'][0]['size_gb'] == 1, 'Expected the existing 1GB /home volume')
    declared = json.loads(Path('machine-config.json').read_text())
    require([c['name'] for c in declared['containers']] == ['cpa', 'cpamp'],
            'Expected both declared containers')
    image_changed = False
    expected_digests = {}
    for container, repository in zip(declared['containers'],
                                     ['eceasy/cli-proxy-api', 'seakee/cpa-manager-plus']):
        image, equivalent = resolve(repository)
        current = next(c for c in live['containers'] if c['name'] == container['name'])
        changed = current['image_ref']['digest'] not in equivalent
        image_changed |= changed
        container['image'] = image
        expected_digests[container['name']] = sorted(equivalent)
        summary('`' + container['name'] + '`: ' + ('update available' if changed else 'unchanged')
                + ' — `' + image + '`')
    do_deploy = image_changed or os.environ.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch')
    output('deploy', str(do_deploy).lower())
    output('machine', live['id'])
    output('image', declared['containers'][0]['image'])
    if not do_deploy:
        summary('No upstream image changed. Deployment and restart skipped.')
        return
    ROOT.mkdir(exist_ok=True)
    (ROOT / 'before.json').write_text(json.dumps(live))
    (ROOT / 'expected.json').write_text(json.dumps(expected_digests))
    # Pin both resolved versions for this deployment; no runtime files enter Git.
    Path('machine-config.json').write_text(json.dumps(declared, indent=2) + '\n')
    summary('Updating the existing Tokyo Machine in place; retaining its volume and app Secrets.')


def snapshot():
    before = json.loads((ROOT / 'before.json').read_text())
    volume = before['config']['mounts'][0]['volume']
    path = 'volumes/' + volume + '/snapshots'
    previous = {s['id'] for s in api(path)}
    subprocess.run(['flyctl', 'volumes', 'snapshots', 'create', volume, '--app', APP], check=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        fresh = [s for s in api(path) if s['id'] not in previous and s.get('status') == 'created']
        if fresh:
            summary('Pre-deployment volume snapshot is ready.')
            return
        time.sleep(5)
    raise RuntimeError('Snapshot did not become ready; deployment aborted')


def probe(url, expected):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    require(status == expected, 'Unexpected HTTP status for ' + url + ': ' + str(status))


def verify():
    before = json.loads((ROOT / 'before.json').read_text())
    expected = json.loads((ROOT / 'expected.json').read_text())
    deadline = time.monotonic() + 180
    while True:
        live = api('machines/' + before['id'])
        checks = live.get('checks', [])
        ready = live['state'] == 'started' and len(checks) >= 2 and all(
            check['status'] == 'passing' for check in checks)
        if ready:
            break
        require(time.monotonic() < deadline, 'Both service checks did not pass')
        time.sleep(5)
    require(len(api('machines')) == 1, 'Machine count changed')
    require(live['region'] == before['region'], 'Region changed')
    for key in ['mounts', 'guest', 'init', 'services', 'stop_config', 'restart']:
        require(live['config'].get(key) == before['config'].get(key), key + ' changed unexpectedly')
    for container in live['config']['containers']:
        original = next(c for c in before['config']['containers'] if c['name'] == container['name'])
        runtime = next(c for c in live['containers'] if c['name'] == container['name'])
        require(runtime['image_ref']['digest'] in expected[container['name']], 'Image digest mismatch')
        a, b = copy.deepcopy(original), copy.deepcopy(container)
        a.pop('image'); b.pop('image')
        require(a == b, container['name'] + ' startup, environment or Secret mapping changed')
    probe('https://' + APP + '.fly.dev/', 200)
    probe('https://' + APP + '.fly.dev/v1/models', 401)
    probe('https://' + APP + '.fly.dev:8443/', 200)
    summary('Verified both images, both TCP checks, HTTP endpoints, Tokyo region, original '
            'volume, resources, swap, startup commands, Secret mappings and autostop settings.')


if __name__ == '__main__':
    {'prepare': prepare, 'snapshot': snapshot, 'verify': verify}[sys.argv[1]]()
