#!/usr/bin/env python3
"""Bounded weekly discovery and preparation; publication is a separate verified step."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import prepare_public_sound_candidates as packs
import sound_fingerprint_index as fingerprints
import sync_public_sound_sources as sources

ROOT = Path(__file__).resolve().parents[2]


class BudgetClient(sources.Client):
    def __init__(self, folder, maximum_bytes=256 * 1024 * 1024, maximum_requests=160):
        super().__init__(folder)
        self.maximum_bytes, self.maximum_requests = maximum_bytes, maximum_requests
        self.bytes = self.requests = 0

    def get(self, url, maximum=8 * 1024 * 1024, cache=True, referer=None):
        if self.requests >= self.maximum_requests or self.bytes >= self.maximum_bytes:
            raise ValueError('Scheduled source download budget exhausted')
        self.requests += 1
        allowed = min(maximum, self.maximum_bytes - self.bytes)
        # Reserve before I/O: a rejected oversized response still consumes budget.
        self.bytes += allowed
        result = super().get(url, allowed, cache, referer)
        if len(result) > allowed:
            raise ValueError('Source violated reserved download budget')
        self.bytes -= allowed - len(result)
        return result


def eligible(item):
    return item['id'] in {'kenney:impact-sounds', 'kenney:rpg-audio'} or packs.classify(item['title']) is not None


def profile_available(profile, ids):
    # Do not republish one alternate take per week as a new collection.
    return not any(ident.startswith('source-' + profile['id'] + '-') for ident in ids)


def release_tag(candidates):
    seed = {'packagerSha256': fingerprints.sha(Path(packs.__file__).read_bytes()),
            'candidates': sorted((c['profile']['id'], c['clip']['sha256']) for c in candidates)}
    return 'scenevox-resources-v' + str(int(fingerprints.sha(fingerprints.encoded(seed))[:14], 16))


def prepare(args):
    before = args.catalogue.read_bytes()
    previous = args.index.read_bytes()
    baseline = fingerprints.load(previous, before)
    published = json.loads(before)
    args.work.mkdir(parents=True, exist_ok=True)
    output = args.work / 'candidate'
    if output.exists() and any(output.iterdir()):
        raise ValueError('Candidate output must be empty; use a fresh work directory')
    if args.cache.exists() and sum(p.stat().st_size for p in args.cache.rglob('*') if p.is_file()) > 768 * 1024 * 1024:
        raise ValueError('Dedicated source cache capacity exceeded')
    inventory_path = args.cache / 'inventory.json'
    old_inventory = json.loads(inventory_path.read_bytes()) if inventory_path.exists() else {}
    client = BudgetClient(args.cache / 'http-cache', args.max_download_mib * 1024 * 1024, args.max_requests)
    new, rejected, scans = [], [], []
    if args.offline:
        inventory = old_inventory
        if not inventory.get('items'):
            raise ValueError('Offline run needs a staged source inventory')
    else:
        for source in ('commons', 'kenney'):
            rows, refused, scanned = sources.commons_index(client, sources.CATEGORIES, 1) if source == 'commons' else sources.kenney_index(client)
            new.extend(rows); rejected.extend(refused); scans.extend(scanned)
        inventory, delta = sources.merge(old_inventory, new, rejected)
        sources.atomic(inventory_path, sources.document(inventory))
        sources.atomic(args.work / 'discovery.json', sources.document(dict(changes=delta, excluded=rejected, scans=scans)))
        def needed(item):
            if not eligible(item): return False
            profile = packs.classify(item['title'])
            if profile is not None: return profile_available(profile, baseline['packIds'])
            return any(profile_available(p, baseline['packIds']) for p in packs.PROFILES)
        changed = set(delta['added'] + delta['changed'])
        choices = sorted((x for x in inventory['items'] if needed(x)), key=lambda x: (x['id'] not in changed, x['id']))
        for item in choices[:args.max_sources]:
            sources.stage(client, item, args.cache)
        sources.atomic(args.work / 'deferred-sources.json', sources.document({'ids': [x['id'] for x in choices[args.max_sources:]]}))
    if sum(p.stat().st_size for p in args.cache.rglob('*') if p.is_file()) > 768 * 1024 * 1024:
        raise ValueError('Dedicated source cache capacity exceeded')
    candidates, skipped = packs.choose_clips(args.cache / 'staged', set(baseline['originalSha256s']), {x['id']: x for x in inventory['items']})
    candidates = [c for c in candidates if profile_available(c['profile'], baseline['packIds'])]
    selected, deferred = candidates[:args.max_packs], candidates[args.max_packs:]
    output.mkdir(parents=True, exist_ok=True)
    tag = release_tag(selected)
    items, contexts, rejected_audio = packs.build(selected, output, tag,
        (set(baseline['originalSha256s']), set(baseline['pcmSha256s']), set(baseline['packIds'])))
    increment = sources.document({'schemaVersion': 1, 'items': items})
    sources.atomic(output / 'increment.json', increment)
    sources.atomic(output / 'review.json', sources.document(dict(unmappedOrDuplicate=skipped, rejectedAudio=rejected_audio,
        deferredProfiles=[c['profile']['id'] for c in deferred], unknownSources=[x['id'] for x in inventory['items'] if not eligible(x)])))
    report = dict(version=1, addedPacks=len(items), releaseTag=tag, baselineSha256=fingerprints.sha(before),
        baselineIndexSha256=fingerprints.sha(previous), incrementSha256=fingerprints.sha(increment),
        downloadedBytes=client.bytes, requests=client.requests, readyForPublication=False,
        requiresAndroidImportVerification=bool(items), state='no-change' if not items else 'prepared', published=False)
    if items:
        subprocess.run([sys.executable, str(ROOT / 'tools/sound-packs/assess_audio.py'), '--catalogue', str(output / 'increment.json'),
            '--folder', str(output / 'packs'), '--output', str(output / 'audio-qa.json')], check=True)
        merged = sources.document({'schemaVersion': 1, 'items': published['items'] + items})
        sources.atomic(output / 'catalogue.pending.json', merged)
        subprocess.run([sys.executable, str(ROOT / 'tools/resources/publish/publish_catalogue.py'), '--kind', 'sounds',
            '--catalogue', str(output / 'catalogue.pending.json')], check=True)
        index = fingerprints.build(merged, output / 'packs', before, previous)
        sources.atomic(output / 'fingerprints.pending.json', index)
    if args.catalogue.read_bytes() != before or args.index.read_bytes() != previous:
        raise ValueError('Baseline changed during preparation')
    sources.atomic(output / 'schedule-report.json', sources.document(report))
    print(json.dumps(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', type=Path, required=True)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--max-sources', type=int, default=4, choices=range(1, 9))
    parser.add_argument('--max-packs', type=int, default=12, choices=range(1, 25))
    parser.add_argument('--max-download-mib', type=int, default=256, choices=range(1, 513))
    parser.add_argument('--max-requests', type=int, default=160, choices=range(1, 241))
    parser.add_argument('--offline', action='store_true')
    prepare(parser.parse_args())


if __name__ == '__main__':
    main()
