#!/usr/bin/env python3
"""Build or increment a small, catalogue-bound source/PCM deduplication index."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

MAX_INDEX = 2 * 1024 * 1024
SHA = re.compile('[0-9a-f]{64}')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def load(raw, catalogue):
    if len(raw) > MAX_INDEX:
        raise ValueError('Fingerprint index capacity')
    value = json.loads(raw)
    data = json.loads(catalogue)
    ids = [x['id'] for x in data['items']]
    if value.get('version') != 1 or value.get('catalogueSha256') != sha(catalogue):
        raise ValueError('Fingerprint catalogue mismatch')
    if sorted(value.get('packIds', [])) != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError('Fingerprint pack identities mismatch')
    for key in ('originalSha256s', 'pcmSha256s'):
        rows = value.get(key)
        if not isinstance(rows, list) or any(not isinstance(s, str) or not SHA.fullmatch(s) for s in rows) or len(rows) != len(set(rows)):
            raise ValueError('Invalid fingerprints: ' + key)
    return value


def pack_hashes(item, folder):
    filename = item['packageUrl'].rsplit('/', 1)[-1]
    if not re.fullmatch('[A-Za-z0-9._-]+[.]zip', filename):
        raise ValueError('Invalid archive filename')
    path = folder / filename
    data = path.read_bytes()
    if len(data) != item['bytes'] or sha(data) != item['sha256']:
        raise ValueError('Archive changed: ' + item['id'])
    original, pcm = set(), set()
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('pack.json'))
        source = json.loads(archive.read('SOURCE.json'))
        if manifest['id'] != item['id'] or len(manifest['sounds']) > 2048:
            raise ValueError('Archive identity/capacity')
        for clip in manifest['sounds']:
            entry = archive.getinfo(clip['file'])
            if entry.file_size > 64 * 1024 * 1024:
                raise ValueError('Audio entry capacity')
            actual = sha(archive.read(entry))
            if actual != clip['sha256']:
                raise ValueError('Audio changed')
            pcm.add(actual)
        for recording in source.get('recordings', []):
            values = [recording.get(k, '') for k in ('originalSha256', 'sourceSha256')]
            values += recording.get('originalSha256s', [])
            original.update(v for v in values if isinstance(v, str) and SHA.fullmatch(v))
    return original, pcm


def build(catalogue, folder, old_catalogue=None, previous=None):
    current = json.loads(catalogue)
    original, pcm, old_ids = set(), set(), set()
    if previous is not None:
        index = load(previous, old_catalogue)
        old = json.loads(old_catalogue)
        old_items = {x['id']: x for x in old['items']}
        new_items = {x['id']: x for x in current['items']}
        if any(new_items.get(key) != item for key, item in old_items.items()):
            raise ValueError('Increment changed an existing resource')
        original.update(index['originalSha256s']); pcm.update(index['pcmSha256s']); old_ids.update(index['packIds'])
    for item in current['items']:
        if item['id'] in old_ids:
            continue
        a, b = pack_hashes(item, folder)
        original.update(a); pcm.update(b)
    value = dict(version=1, catalogueSha256=sha(catalogue), packIds=sorted(x['id'] for x in current['items']),
                 originalSha256s=sorted(original), pcmSha256s=sorted(pcm))
    raw = encoded(value)
    load(raw, catalogue)
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', type=Path, required=True)
    parser.add_argument('--packs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous-catalogue', type=Path)
    parser.add_argument('--previous-index', type=Path)
    args = parser.parse_args()
    if bool(args.previous_catalogue) != bool(args.previous_index):
        parser.error('Previous catalogue and index must be supplied together')
    raw = build(args.catalogue.read_bytes(), args.packs,
                args.previous_catalogue.read_bytes() if args.previous_catalogue else None,
                args.previous_index.read_bytes() if args.previous_index else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    print(json.dumps({'bytes': len(raw), 'sha256': sha(raw), 'packs': len(json.loads(raw)['packIds'])}))


if __name__ == '__main__':
    main()
