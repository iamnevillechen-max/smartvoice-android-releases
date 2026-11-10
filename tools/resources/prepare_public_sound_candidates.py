#!/usr/bin/env python3
"""Daily/manual fixed-source discovery -> deduplicated packs -> existing PCM acceptance.

Run with .toolchain/audio-qa/bin/python. This is the scheduling entry point; it
does not install a scheduler or publish. Known filename profiles are versioned
below; unknown recordings remain in review.json, never silently assigned a cue.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

import sync_public_sound_sources as sources

ROOT = sources.ROOT
SOUND_TOOLS = ROOT / 'tools/sound-packs'
BASELINE = ROOT / 'distribution/scenevox-sounds-v3.json'
BASELINE_PACKS = ROOT / 'dist/sound-resources'
EXPECTED_EVENTS = {
    'carpet-step': ['Walk, footsteps'], 'concrete-step': ['Walk, footsteps'],
    'grass-step': ['Walk, footsteps'], 'snow-step': ['Walk, footsteps'], 'wood-step': ['Walk, footsteps'],
    'metal-impact': ['Clang', 'Bang', 'Thump, thud'], 'glass-impact': ['Chink, clink', 'Glass'],
    'wood-impact': ['Thump, thud', 'Knock', 'Tap'], 'door-knocker': ['Knock'],
    'door-opening': ['Door'], 'door-closing': ['Door', 'Slam'],
}


def profile(key, pattern, name, zh, hant, terms, groups, category='Foley', release=700):
    return dict(id=key, pattern=pattern, name=name, nameZh=zh, nameHant=hant,
                keywords=terms, allOf=groups, category=category, releaseMs=release)


# Match original filenames, not a remote webpage description or guessed genre.
# Each profile chooses one variant, so installing a package never plays several
# near-identical copies of the same cue at once.
PROFILES = [
    profile('carpet-step', r'^footstep_carpet_\d+$', 'A Step on Carpet', '踩在地毯上', '踩在地毯上',
            ['踩在地毯上', '踏上地毯', 'stepped on the carpet', 'footstep on carpet'],
            [['地毯', 'carpet'], ['踩', '踏', 'step', 'footstep']]),
    profile('concrete-step', r'^footstep_concrete_\d+$', 'A Step on Concrete', '踩在水泥地上', '踩在水泥地上',
            ['踩在水泥地上', '踩上水泥地', 'stepped on concrete', 'footstep on concrete'],
            [['水泥地', 'concrete'], ['踩', '踏', 'step', 'footstep']]),
    profile('grass-step', r'^footstep_grass_\d+$', 'A Step on Grass', '踩在草地上', '踩在草地上',
            ['踩在草地上', '踏上草地', 'stepped on the grass', 'footstep on grass'],
            [['草地', 'grass'], ['踩', '踏', 'step', 'footstep']]),
    profile('snow-step', r'^footstep_snow_\d+$', 'A Step in Snow', '踩进积雪', '踩進積雪',
            ['踩进积雪', '踩進積雪', '踩在雪地上', 'stepped into snow', 'footstep in snow'],
            [['积雪', '積雪', '雪地', 'snow'], ['踩', '踏', 'step', 'footstep']], category='Nature'),
    profile('wood-step', r'^footstep_wood_\d+$', 'A Step on Wooden Boards', '踩在木板上', '踩在木板上',
            ['踩在木板上', '踩上木地板', 'stepped on wooden boards', 'footstep on wood'],
            [['木板', '木地板', 'wooden', 'wood'], ['踩', '踏', 'step', 'footstep']]),
    profile('metal-impact', r'^impactMetal_medium_\d+$', 'Metal Striking Metal', '金属碰响', '金屬碰響',
            ['金属撞击', '金屬撞擊', '金属碰响', '金屬碰響', 'metal struck metal', 'metal clanged'],
            [['金属', '金屬', 'metal'], ['撞', '碰', 'struck', 'clang']]),
    profile('glass-impact', r'^impactGlass_medium_\d+$', 'Glass Contact', '玻璃碰响', '玻璃碰響',
            ['玻璃碰响', '玻璃碰響', '玻璃杯相碰', 'glass clinked', 'glasses clinked'],
            [['玻璃', 'glass'], ['碰', 'clink']]),
    profile('wood-impact', r'^impactWood_medium_\d+$', 'Wooden Impact', '木头碰撞', '木頭碰撞',
            ['木头碰撞', '木頭碰撞', '木板撞响', '木板撞響', 'wood knocked against wood'],
            [['木头', '木頭', '木板', 'wood'], ['撞', '碰', 'knock']]),
    profile('door-knocker', r'^(?:Door knocker audio|doorKnock_?\d*)$', 'Door Knocker', '门环叩响', '門環叩響',
            ['叩响门环', '叩響門環', '敲响门环', '敲響門環', '门环叩响', '門環叩響', 'struck the door knocker'],
            [['门环', '門環', 'door knocker'], ['叩', '敲', 'struck', 'knock']]),
    profile('door-opening', r'^doorOpen_\d+$', 'Door Opening', '推开房门', '推開房門',
            ['推开房门', '推開房門', '打开房门', '打開房門', 'opened the door'],
            [['房门', '房門', 'door'], ['推开', '推開', '打开', '打開', 'opened']]),
    profile('door-closing', r'^doorClose_\d+$', 'Door Closing', '关上房门', '關上房門',
            ['关上房门', '關上房門', '房门关上', '房門關上', 'closed the door'],
            [['房门', '房門', 'door'], ['关上', '關上', 'closed']]),
]


def classify(original_path):
    name = Path(original_path).stem
    matches = [p for p in PROFILES if re.fullmatch(p['pattern'], name, re.I)]
    return matches[0] if len(matches) == 1 else None


def baseline_fingerprints(catalogue, folder, index_path=None, catalogue_sha256=None):
    if index_path is not None:
        if index_path.stat().st_size>2*1024*1024:raise ValueError('Baseline index capacity')
        index=json.loads(index_path.read_bytes())
        if not catalogue_sha256 or index.get('version')!=1 or index.get('catalogueSha256')!=catalogue_sha256:
            raise ValueError('Baseline index catalogue hash differs')
        result=[]
        for key in ['originalSha256s','pcmSha256s','packIds']:
            values=index.get(key)
            if not isinstance(values,list) or len(values)>50000 or any(not isinstance(x,str)for x in values) or len(values)!=len(set(values)):
                raise ValueError('Baseline index values')
            if key!='packIds' and any(not re.fullmatch('[0-9a-f]{64}',x)for x in values):raise ValueError('Baseline index hash')
            result.append(set(values))
        if result[2]!={item['id']for item in catalogue['items']}:raise ValueError('Baseline index package identity differs')
        return tuple(result)
    original, pcm, ids = set(), set(), set()
    for item in catalogue['items']:
        path = folder / item['packageUrl'].rsplit('/', 1)[1]
        data = path.read_bytes()
        if len(data) != item['bytes'] or sources.digest(data) != item['sha256']:
            raise ValueError('Baseline archive changed: ' + item['id'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read('pack.json'))
            record = json.loads(archive.read('SOURCE.json'))
            ids.add(manifest['id'])
            for clip in manifest['sounds']:
                actual = sources.digest(archive.read(clip['file']))
                if actual != clip['sha256']:
                    raise ValueError('Baseline PCM changed')
                pcm.add(actual)
            for entry in record.get('recordings', []):
                for field in ('originalSha256', 'sourceSha256'):
                    if re.fullmatch('[0-9a-f]{64}', entry.get(field, '')):
                        original.add(entry[field])
                original.update(entry.get('originalSha256s', []))
    return original, pcm, ids


def choose_clips(staged, original_hashes, eligible=None):
    chosen, skipped = {}, []
    for folder in sorted(staged.glob('*')):
        proof = folder / 'SOURCE.json'
        if not proof.exists():
            continue
        record = json.loads(proof.read_bytes())
        if record['source']['license'] != 'CC0-1.0':
            raise ValueError('Staged licence changed')
        current = eligible.get(record['source']['id']) if eligible is not None else record['source']
        if current is None or (current.get('sourceSha1') and current['sourceSha1'] != record['source'].get('sourceSha1')):
            skipped.append(dict(source=record['source']['id'], originalPath='', reason='source-no-longer-current'))
            continue
        for clip in sorted(record['clips'], key=lambda row: row['originalPath']):
            category = classify(clip['originalPath'])
            reason = 'original-already-published' if clip['sha256'] in original_hashes else 'unmapped-filename' if category is None else 'alternate-variant' if category['id'] in chosen else None
            if reason:
                skipped.append(dict(source=record['source']['id'], originalPath=clip['originalPath'], reason=reason))
                continue
            data = (folder / clip['file']).read_bytes()
            if len(data) != clip['bytes'] or sources.digest(data) != clip['sha256']:
                raise ValueError('Staged clip changed')
            chosen[category['id']] = dict(profile=category, clip=clip, source=record['source'], path=folder / clip['file'])
    return list(chosen.values()), skipped


def write_pack(path, entries):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries):
            entry = zipfile.ZipInfo(name, (2026, 11, 10, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)


def build(candidates, destination, release_tag, baseline):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    import math
    sys.path.insert(0, str(SOUND_TOOLS))
    import prepare
    import assess_audio
    _, pcm_seen, ids = baseline
    folder = destination / 'packs'
    folder.mkdir(parents=True, exist_ok=True)
    items, contexts, skipped, classification = [], [], [], []
    for candidate in candidates:
        p, clip, source = candidate['profile'], candidate['clip'], candidate['source']
        samples, rate = sf.read(candidate['path'], dtype='float64', always_2d=True)
        mono = samples.mean(axis=1)
        if not len(mono) or not np.isfinite(mono).all():
            skipped.append(dict(id=p['id'], reason='invalid-audio'))
            continue
        peak = float(np.max(np.abs(mono)))
        active = np.flatnonzero(np.abs(mono) > max(.001, peak * .01))
        if peak < .003 or not active.size:
            skipped.append(dict(id=p['id'], reason='silent-audio'))
            continue
        start = max(0, int(active[0]) - round(rate * .025))
        end = min(len(mono), int(active[-1]) + round(rate * .1), start + 8 * rate)
        signal = mono[start:end]
        divisor = math.gcd(rate, prepare.RATE)
        signal = resample_poly(signal, prepare.RATE // divisor, rate // divisor, window=('kaiser', 8.6))
        signal *= min(4.0, .70 / max(1e-9, np.max(np.abs(signal))))
        edge = min(128, len(signal) // 8)
        if edge:
            signal[:edge] *= np.linspace(0, 1, edge)
            signal[-edge:] *= np.linspace(1, 0, edge)
        data = prepare.wav(signal)
        checksum = sources.digest(data)
        if checksum in pcm_seen:
            skipped.append(dict(id=p['id'], reason='pcm-already-published'))
            continue
        if not assess_audio.usable(assess_audio.signal(data)):
            skipped.append(dict(id=p['id'], reason='pcm-quality'))
            continue
        identifier = 'source-' + p['id'] + '-' + checksum[:10]
        if identifier in ids:
            raise ValueError('Candidate identity collision')
        pcm_seen.add(checksum)
        ids.add(identifier)
        sound_id = identifier + '-clip'
        name = 'audio/' + sound_id + '.wav'
        sound = dict(id=sound_id, name=p['name'], nameZh=p['nameZh'], nameHant=p['nameHant'], file=name,
                     sha256=checksum, keywords=p['keywords'], position=1, distance=1.5, volume=.4,
                     attackMs=25, releaseMs=p['releaseMs'], mood='any')
        context = dict(id=identifier + '-context', soundId=sound_id, packId=identifier, sha256=checksum,
                       keywordForms=['\n'.join(p['keywords'])], gateLiterals=True, windowChars=72, priority=55,
                       patterns=[dict(allOf=p['allOf'], cueGroup=0)])
        manifest = dict(schemaVersion=1, id=identifier, name=p['name'], nameZh=p['nameZh'], nameHant=p['nameHant'],
                        description=p['name'] + '.', descriptionZh=p['nameZh'] + '。', descriptionHant=p['nameHant'] + '。',
                        category=p['category'], categoryZh='自然' if p['category'] == 'Nature' else '动作',
                        categoryHant='自然' if p['category'] == 'Nature' else '動作', license='CC0-1.0', sounds=[sound],
                        contexts=dict(version=1, rules=[context]))
        provenance = dict(license='CC0-1.0', sources={source['id']: source}, recordings=[dict(
            originalPath=clip['originalPath'], originalSha256=clip['sha256'], originalBytes=clip['bytes'],
            startSeconds=start / rate, selectedSeconds=len(signal) / prepare.RATE, sourceSampleRate=rate,
            outputFile=name, outputSha256=checksum, outputBytes=len(data),
            conversion='Original speed, equal-gain mono, Kaiser resampling, bounded linear gain and click-edge fades')])
        archive = folder / (identifier + '-v1.zip')
        write_pack(archive, [('pack.json', sources.document(manifest)), ('SOURCE.json', sources.document(provenance)),
                             ('LICENSE.txt', ('CC0-1.0\n' + sources.CC0 + '\n').encode()), (name, data)])
        preview = prepare.spatial(signal, sound['position'], sound['distance'], sound['volume'], sound['attackMs'], sound['releaseMs'])
        # Audition gain is independent of the quieter speech-mixing gain.
        preview *= min(4.0, .7 / max(1e-9, np.max(np.abs(preview))))
        sample = folder / (identifier + '-preview.wav')
        sf.write(sample, preview, prepare.RATE, subtype='PCM_16')
        base_url = 'https://github.com/iamnevillechen-max/smartvoice-android-releases/releases/download/' + release_tag + '/'
        item = {key: manifest[key] for key in ('id', 'name', 'nameZh', 'nameHant', 'description', 'descriptionZh', 'descriptionHant')}
        item.update(engine=p['category'], language='und', packageUrl=base_url + archive.name,
                    bytes=archive.stat().st_size, sha256=sources.digest(archive.read_bytes()), previewUrl=base_url + sample.name,
                    previewBytes=sample.stat().st_size, previewSha256=sources.digest(sample.read_bytes()),
                    contexts=manifest['contexts'])
        if item['previewBytes'] > 5 * 1024 * 1024:
            raise ValueError('Preview capacity')
        items.append(item)
        contexts.append(context)
        classification_file = destination / 'classification' / (identifier + '.wav')
        sources.atomic(classification_file, data)
        classification.append(dict(id=identifier, path=str(classification_file.resolve()),
                  sourceLabels=[source['title'], clip['originalPath']],
                  expectedEvents=EXPECTED_EVENTS[p['id']]))
    sources.atomic(destination / 'audio-classification-input.json', sources.document(dict(items=classification)))
    return items, contexts, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=sources.DEFAULT_OUTPUT)
    parser.add_argument('--output', type=Path, default=ROOT / 'tmp/public-sound-candidate')
    parser.add_argument('--release-tag', required=True)
    parser.add_argument('--sync', action='store_true', help='Refresh fixed public source directories first')
    parser.add_argument('--stage-known', action='store_true', help='Download only the fixed sources with supported filename profiles')
    parser.add_argument('--audio-classification', action='store_true', help='Run optional local YAMNet review; does not auto-publish')
    parser.add_argument('--baseline-index',type=Path,help='Verified deduplication index bound to the exact current catalogue bytes')
    parser.add_argument('--baseline-catalogue',type=Path,default=BASELINE,help='Current published sound catalogue')
    args = parser.parse_args()
    if not re.fullmatch(r'scenevox-resources-v[0-9]+', args.release_tag):
        parser.error('Use an explicit resource release tag')
    before = args.baseline_catalogue.read_bytes()
    published = json.loads(before)
    baseline = baseline_fingerprints(published, BASELINE_PACKS,args.baseline_index,sources.digest(before))
    if args.sync:
        subprocess.run([sys.executable, str(Path(sources.__file__)), 'sync', '--output', str(args.sources)], check=True)
    stage_failures = []
    if args.stage_known:
        inventory = json.loads((args.sources / 'inventory.json').read_bytes())
        client = sources.Client(args.sources / 'http-cache')
        for item in inventory['items']:
            if item['id'] not in {'kenney:impact-sounds', 'kenney:rpg-audio'} and classify(item['title']) is None:
                continue
            try:
                sources.stage(client, item, args.sources)
            except Exception as failure:
                stage_failures.append(dict(id=item['id'], error=str(failure)))
    inventory = json.loads((args.sources / 'inventory.json').read_bytes())
    candidates, skipped = choose_clips(args.sources / 'staged', baseline[0], {item['id']: item for item in inventory['items']})
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'catalogue.pending.json').unlink(missing_ok=True)
    sources.atomic(args.output / 'prepare-report.json', sources.document(dict(readyForPublication=False, state='building', published=False)))
    items, contexts, rejected = build(candidates, args.output, args.release_tag, baseline)
    increment = dict(schemaVersion=1, items=items)
    sources.atomic(args.output / 'increment.json', sources.document(increment))
    sources.atomic(args.output / 'context-candidates.json', sources.document(dict(version=1, rules=contexts)))
    sources.atomic(args.output / 'review.json', sources.document(dict(unmappedOrDuplicate=skipped, rejectedAudio=rejected, sourceFailures=stage_failures)))
    if items and args.audio_classification:
        subprocess.run([str(ROOT / '.toolchain/sound-classification/bin/python'), str(ROOT / 'tools/resources/classify_audio.py'),
                        '--input', str(args.output / 'audio-classification-input.json'),
                        '--output', str(args.output / 'audio-classification.json')], check=True)
    if items:
        subprocess.run([sys.executable, str(SOUND_TOOLS / 'assess_audio.py'), '--catalogue', str(args.output / 'increment.json'),
                        '--folder', str(args.output / 'packs'), '--baseline-folder', str(BASELINE_PACKS),
                        '--output', str(args.output / 'audio-qa.json')], check=True)
        subprocess.run([sys.executable, str(ROOT / 'tools/resources/publish/publish_catalogue.py'), '--kind', 'sounds',
                        '--catalogue', str(args.output / 'increment.json')], check=True)
    merged = dict(schemaVersion=1, items=published['items'] + items)
    sources.atomic(args.output / 'catalogue.pending.json', sources.document(merged))
    subprocess.run([sys.executable, str(ROOT / 'tools/resources/publish/publish_catalogue.py'), '--kind', 'sounds',
                    '--catalogue', str(args.output / 'catalogue.pending.json')], check=True)
    if args.baseline_catalogue.read_bytes() != before:
        raise ValueError('Public baseline changed concurrently; rerun before release')
    report = dict(readyForImportTest=bool(items) and not stage_failures, readyForPublication=False,
                  requiresAndroidImportVerification=True, published=False, addedPacks=len(items),
                  baselinePacks=len(published['items']), candidatePacks=len(merged['items']),
                  originalDuplicateCount=sum(x['reason'] == 'original-already-published' for x in skipped),
                  unknownCount=sum(x['reason'] == 'unmapped-filename' for x in skipped), sourceFailures=stage_failures,
                  baselineSha256=sources.digest(before), incrementSha256=sources.digest(sources.document(increment)))
    sources.atomic(args.output / 'prepare-report.json', sources.document(report))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
