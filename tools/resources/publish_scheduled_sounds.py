#!/usr/bin/env python3
"""Publish only prepared sound assets and atomically advance the sound feed/index."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import zipfile

import sound_fingerprint_index as fingerprints

REPO = 'iamnevillechen-max/smartvoice-android-releases'
FEED = 'scenevox-sounds-v3.json'
INDEX = 'scenevox-sound-fingerprints.json'
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('catalogue_validator', Path(__file__).parent / 'publish/publish_catalogue.py')
validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)


def validate_candidate(folder, baseline_raw, index_raw, app_feed):
    report = json.loads((folder / 'schedule-report.json').read_bytes())
    if report.get('addedPacks') == 0:
        return None
    if report.get('baselineSha256') != fingerprints.sha(baseline_raw) or report.get('baselineIndexSha256') != fingerprints.sha(index_raw):
        raise ValueError('Remote resource baseline changed; prepare again')
    baseline = validator.validate(baseline_raw)
    fingerprints.load(index_raw, baseline_raw)
    inc_raw = (folder / 'increment.json').read_bytes(); increment = validator.validate(inc_raw)
    new_raw = (folder / 'catalogue.pending.json').read_bytes(); merged = validator.validate(new_raw)
    next_index = (folder / 'fingerprints.pending.json').read_bytes(); fingerprints.load(next_index, new_raw)
    if merged['items'] != baseline['items'] + increment['items'] or not 0 < len(increment['items']) <= 24:
        raise ValueError('Only append-only bounded resources may be published')
    if fingerprints.sha(inc_raw) != report['incrementSha256'] or len(increment['items']) != report['addedPacks']:
        raise ValueError('Increment identity mismatch')
    qa = json.loads((folder / 'audio-qa.json').read_bytes())
    if qa.get('passed') is not True or not qa.get('items') or any(x.get('passed') is not True for x in qa['items']):
        raise ValueError('PCM acceptance missing')
    attestation = json.loads((folder / 'android-attestation.json').read_bytes())
    app = json.loads(app_feed)
    if app.get('packageName') != 'com.scenevox.tts' or int(app.get('versionCode', 0)) < 103:
        raise ValueError('Published APK does not yet support candidate contexts')
    if any(attestation.get(key) != value for key, value in {
        'version': 1, 'passed': True, 'packageName': 'com.scenevox.tts',
        'apkSha256': app['sha256'], 'apkVersionCode': app['versionCode'],
        'incrementSha256': fingerprints.sha(inc_raw), 'audioQaSha256': fingerprints.sha((folder / 'audio-qa.json').read_bytes()),
        'verifiedPackIds': sorted(x['id'] for x in increment['items']), 'testedProductionApk': True,
        'publishedApk': True}.items()):
        raise ValueError('Exact published APK/resource Android attestation missing or stale')
    tag = report['releaseTag']
    if not re.fullmatch(r'scenevox-resources-v[0-9]+', tag):
        raise ValueError('Invalid resource tag')
    assets = []
    for item in increment['items']:
        for url, length, digest in [('packageUrl', 'bytes', 'sha256'), ('previewUrl', 'previewBytes', 'previewSha256')]:
            prefix = 'https://github.com/' + REPO + '/releases/download/' + tag + '/'
            value = item[url]
            if not value.startswith(prefix) or not re.fullmatch(r'[A-Za-z0-9._-]+[.](zip|wav)', value[len(prefix):]):
                raise ValueError('Resource asset destination is outside its release')
            path = folder / 'packs' / value[len(prefix):]
            data = path.read_bytes()
            if len(data) != item[length] or fingerprints.sha(data) != item[digest]:
                raise ValueError('Prepared asset changed')
            assets.append((path, item[digest], item[length]))
    if len({path.name for path, _, _ in assets}) != len(assets):
        raise ValueError('Duplicate asset names')
    expected = {x['sha256'] for x in qa['items'] if x.get('kind') == 'recording'}
    actual_pcm = set()
    for item in increment['items']:
        actual_pcm.update(fingerprints.pack_hashes(item, folder / 'packs')[1])
        with zipfile.ZipFile(folder / 'packs' / item['packageUrl'].rsplit('/', 1)[-1]) as archive:
            manifest = json.loads(archive.read('pack.json'))
            if item.get('contexts') != manifest.get('contexts'):
                raise ValueError('Catalogue and installed package contexts differ')
    if not expected or expected != actual_pcm or not expected.issubset(set(json.loads(next_index)['pcmSha256s'])):
        raise ValueError('PCM QA does not match fingerprint index')
    return dict(report=report, increment=increment, assets=assets, contents={FEED: new_raw, INDEX: next_index})


class GitHub:
    def call(self, path, body=None, method=None):
        command = ['gh', 'api', 'repos/' + REPO + '/' + path]
        if body is not None:
            command += ['--method', method or 'POST', '--input', '-']
        result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                capture_output=True, text=True, check=True, timeout=120)
        return json.loads(result.stdout)

    def content(self, path, ref):
        return base64.b64decode(self.call('contents/' + path + '?ref=' + ref)['content'])

    def command(self, args):
        return subprocess.run(['gh', *args, '--repo', REPO], capture_output=True, text=True, check=True, timeout=300).stdout


def publish(folder, github):
    report = json.loads((folder / 'schedule-report.json').read_bytes())
    if report['addedPacks'] == 0:
        return {'published': False, 'state': 'no-change'}
    head = github.call('git/ref/heads/main')['object']['sha']
    baseline = github.content(FEED, head); index = github.content(INDEX, head)
    pending = (folder / 'catalogue.pending.json').read_bytes(); pending_index = (folder / 'fingerprints.pending.json').read_bytes()
    if baseline == pending and index == pending_index:
        return {'published': False, 'state': 'already-published', 'commit': head}
    plan = validate_candidate(folder, baseline, index, github.content('scenevox-update.json', head))
    tag = plan['report']['releaseTag']
    try:
        release = json.loads(github.command(['release', 'view', tag, '--json', 'isDraft,assets,tagName']))
    except subprocess.CalledProcessError as error:
        if 'release not found' not in error.stderr.lower() and '404' not in error.stderr:
            raise
        github.command(['release', 'create', tag, '--draft', '--target', head, '--title', 'SceneVox sound resources',
                        '--notes', '\n'.join(item['name'] for item in plan['increment']['items'])])
        release = {'isDraft': True, 'assets': []}
    existing = {x['name']: x for x in release['assets']}
    if set(existing) - {path.name for path, _, _ in plan['assets']}:
        raise ValueError('Release contains assets outside this candidate')
    for path, checksum, size in plan['assets']:
        if path.name in existing:
            old = existing[path.name]
            if old.get('size') != size or old.get('digest') != 'sha256:' + checksum:
                raise ValueError('Existing release asset differs; never overwrite')
        else:
            if not release['isDraft']:
                raise ValueError('Published release missing prepared asset')
            github.command(['release', 'upload', tag, str(path)])
    complete = json.loads(github.command(['release', 'view', tag, '--json', 'isDraft,assets,tagName']))
    actual = {x['name']: x for x in complete['assets']}
    if any(actual.get(p.name, {}).get('digest') != 'sha256:' + digest or actual[p.name].get('size') != size for p, digest, size in plan['assets']):
        raise ValueError('Uploaded resource digest mismatch')
    if github.call('git/ref/heads/main')['object']['sha'] != head:
        raise ValueError('Repository changed during upload; draft retained')
    if complete['isDraft']:
        github.command(['release', 'edit', tag, '--draft=false', '--latest=false'])
    # Two resource files in one commit. No application/update file can enter this tree.
    tree = github.call('git/commits/' + head)['tree']['sha']
    entries = []
    for path, raw in plan['contents'].items():
        blob = github.call('git/blobs', {'encoding': 'base64', 'content': base64.b64encode(raw).decode()})['sha']
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob})
    made = github.call('git/trees', {'base_tree': tree, 'tree': entries})['sha']
    commit = github.call('git/commits', {'message': 'Add verified scene sound resources', 'tree': made, 'parents': [head]})['sha']
    # Git ref update with force=false is a CAS-like fast-forward check against head.
    github.call('git/refs/heads/main', {'sha': commit, 'force': False}, method='PATCH')
    if any(github.content(path, commit) != raw for path, raw in plan['contents'].items()):
        raise RuntimeError('Committed resource data differs')
    return {'published': True, 'state': 'published', 'commit': commit, 'release': tag, 'addedPacks': report['addedPacks']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if not args.publish:
        print(json.dumps({'published': False, 'state': 'prepared-only', 'next': 'exact published APK Android attestation'})); return
    print(json.dumps(publish(args.candidate, GitHub())))


if __name__ == '__main__':
    main()
