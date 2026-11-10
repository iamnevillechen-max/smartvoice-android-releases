#!/usr/bin/env python3
"""Discover official Sherpa assets and prepare supported, new voice identities.

Run with .toolchain/audio-qa/bin/python. Discovery never marks a model playable.
The reviewed LibriTTS high recipe uses the existing production Sherpa importer,
unchanged full-precision model files and a distinct unlisted speaker ID.
Publication requires the public Android TTS report and actual WAV via finalize.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import urllib.request
import wave
import zipfile
import yaml

from prepare_network_packages import MetadataLoader
from sync_public_sound_sources import atomic, document, digest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_API = 'https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/tts-models'
DEFAULT_OUTPUT = ROOT / 'tmp/public-voice-sources'
CATALOGUE = ROOT / 'distribution/multitts-resources.json'
RECIPE = 'vits-piper-en_US-libritts-high'


def load_release(folder, refresh):
    path = folder / 'official-tts-release.json'
    if refresh or not path.exists():
        request = urllib.request.Request(RELEASE_API, headers={'User-Agent': 'SceneVoxTTS-resource-index/1.0', 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('Release metadata capacity')
        release = json.loads(data)
        if release.get('tag_name') != 'tts-models' or release.get('draft'):
            raise ValueError('Unexpected upstream release')
        atomic(path, data)
    return json.loads(path.read_bytes())


def discover(release):
    rows = []
    for asset in release['assets']:
        name = asset['name']
        match = re.fullmatch(r'vits-piper-([a-z]{2}_[A-Z]{2})-(.+)-(high|medium)\.tar\.bz2', name)
        rows.append(dict(assetId=asset['id'], name=name, sourceUrl=asset['browser_download_url'], bytes=asset['size'],
                         sha256=(asset.get('digest') or '').removeprefix('sha256:'), updatedAt=asset.get('updated_at'),
                         sourceFormat='tar.bz2' if name.endswith('.tar.bz2') else 'other',
                         language=match.group(1).replace('_', '-') if match else None,
                         engineFamily='vits-piper' if match else None,
                         precision=None, speakerCount=None, previewAvailable=False,
                         state='supported-recipe-needs-inspection' if name == RECIPE + '.tar.bz2' else 'needs-format-and-license-review'))
    return dict(schemaVersion=1, source=RELEASE_API, releaseId=release['id'], assets=rows, published=False)


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def published_voices(catalogue):
    """Fingerprint model bytes + speaker ID, not a package name or arbitrary code."""
    identities, models, packages, layouts = set(), {}, [], {}
    for item in catalogue['items']:
        if item['engine'] != 'Sherpa ONNX':
            continue
        path = ROOT / 'dist/voice-resources' / item['packageUrl'].rsplit('/', 1)[1]
        if path.stat().st_size != item['bytes'] or file_digest(path) != item['sha256']:
            raise ValueError('Published voice archive changed: ' + item['id'])
        with zipfile.ZipFile(path) as archive:
            config = yaml.load(archive.read('config.yaml'), Loader=MetadataLoader)
            for voice in config.get('sherpa', []):
                parameters = json.loads(voice['param'])
                model_dir = parameters['modelDir']
                prefix = 'sherpa/' + model_dir + '/'
                names = sorted(name for name in archive.namelist() if name.startswith(prefix) and name.endswith('.onnx'))
                if not names:
                    raise ValueError('Published voice has no model')
                layout = [(name, archive.getinfo(name).file_size, archive.getinfo(name).CRC) for name in names]
                if model_dir in layouts and layout != layouts[model_dir]:
                    raise ValueError('Published model directory has conflicting model versions')
                if model_dir not in models:
                    fingerprints = []
                    for name in names:
                        value = hashlib.sha256()
                        with archive.open(name) as stream:
                            for block in iter(lambda: stream.read(1024 * 1024), b''): value.update(block)
                        fingerprints.append(value.hexdigest())
                    models[model_dir] = digest(document(fingerprints))
                    layouts[model_dir] = layout
                identities.add((models[model_dir], int(parameters.get('speakerId', 0))))
            packages.append(item['id'])
    return identities, models, packages


def download_reviewed_archive(folder, release):
    """Download only the fixed reviewed recipe, with the official release digest."""
    asset = next(a for a in release['assets'] if a['name'] == RECIPE + '.tar.bz2')
    expected = (asset.get('digest') or '').removeprefix('sha256:')
    if not re.fullmatch('[a-f0-9]{64}', expected) or not 0 < asset['size'] <= 512 * 1024 * 1024:
        raise ValueError('Official archive digest or size unavailable')
    url = asset['browser_download_url']
    if not url.startswith('https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/'):
        raise ValueError('Unexpected official model URL')
    path = folder / asset['name']
    if path.exists() and path.stat().st_size == asset['size'] and file_digest(path) == expected:
        return path
    temporary = path.with_suffix('.part')
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'SceneVoxTTS-resource-index/1.0'})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open('wb') as output:
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block: break
                total += len(block)
                if total > asset['size']: raise ValueError('Official model exceeds declared size')
                output.write(block)
        if temporary.stat().st_size != asset['size'] or file_digest(temporary) != expected:
            raise ValueError('Official download hash/size differs')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def inspect_archive(path, asset):
    if path.stat().st_size != asset['size'] or file_digest(path) != asset['digest'].removeprefix('sha256:'):
        raise ValueError('Official model archive hash/size differs')
    files = {}
    # Streaming avoids decompressing a 130 MiB model twice on maintenance hosts.
    with tarfile.open(path, 'r|bz2') as source:
        member_count = total_bytes = 0
        for member in source:
            member_count += 1; total_bytes += member.size
            if member_count > 5000 or total_bytes > 1024 * 1024 * 1024:
                raise ValueError('Model archive capacity')
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or name.parts[0] != RECIPE:
                raise ValueError('Model archive path')
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Nonregular model entry')
            relative = PurePosixPath(*name.parts[1:])
            if relative.suffix in {'.py', '.sh', '.bat'}:
                continue
            if relative.as_posix() in files:
                raise ValueError('Duplicate model file')
            files[relative.as_posix()] = source.extractfile(member).read()
    model_names = sorted(name for name in files if name.endswith('.onnx'))
    configs = [name for name in files if name.endswith('.onnx.json')]
    if len(model_names) != 1 or len(configs) != 1 or 'tokens.txt' not in files or not any(name.startswith('espeak-ng-data/') for name in files):
        raise ValueError('Unsupported production VITS layout')
    config = json.loads(files[configs[0]])
    card = files.get('MODEL_CARD', b'').decode()
    if config.get('dataset') != 'libritts' or config['audio']['quality'] != 'high' or config['language']['code'] != 'en_US' or 'License: CC BY 4.0' not in card:
        raise ValueError('Reviewed model contract changed')
    count = config['num_speakers']
    if type(count) is not int or not 1 <= count <= 5000 or set(config['speaker_id_map'].values()) != set(range(count)):
        raise ValueError('Invalid speaker manifest')
    fingerprint = digest(document([digest(files[name]) for name in model_names]))
    return files, config, fingerprint


def write_zip(path, entries):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        directories = set()
        for name in entries:
            parent = PurePosixPath(name).parent
            while str(parent) != '.':
                directories.add(parent.as_posix() + '/')
                parent = parent.parent
        for name in sorted(directories):
            info = zipfile.ZipInfo(name, (2026, 11, 10, 0, 0, 0)); info.external_attr = 0o40755 << 16
            archive.writestr(info, b'')
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (2026, 11, 10, 0, 0, 0)); info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def prepare(folder, archive_path, release, catalogue, requested_speaker=None):
    asset = next(a for a in release['assets'] if a['name'] == RECIPE + '.tar.bz2')
    print(json.dumps(dict(phase='inspect-official-model', archive=str(archive_path))), flush=True)
    files, config, fingerprint = inspect_archive(archive_path, asset)
    print(json.dumps(dict(phase='deduplicate-published-models', packages=len(catalogue['items']))), flush=True)
    existing, models, packages = published_voices(catalogue)
    available = [n for n in range(config['num_speakers']) if (fingerprint, n) not in existing]
    atomic(folder / 'model-review.json', document(dict(model=RECIPE, modelFingerprint=fingerprint,
              language='en-US', productionEngine='sherpa', sampleRate=config['audio']['sample_rate'],
              modelSpeakerCount=config['num_speakers'], existingModelSpeakerIds=[n for n in range(config['num_speakers']) if n not in available],
              unlistedModelSpeakerIds=available, sourceArchiveSha256=asset['digest'].removeprefix('sha256:'),
              sourceFormat='tar.bz2', importFormat='config.yaml plus sherpa model directory',
              remainingVoicesNeedNativePreview=True, published=False)))
    if requested_speaker is not None and requested_speaker not in available:
        raise ValueError('Speaker already published or out of range')
    if not available:
        atomic(folder / 'coverage.json', document(dict(model=RECIPE, allSpeakersAlreadyPublished=True)))
        return
    speaker_id = requested_speaker if requested_speaker is not None else available[0]
    code = 'libritts-high-' + str(speaker_id).zfill(3)
    if any(v.get('group') == 'sherpa' and v.get('code') == code for item in catalogue['items'] for v in item.get('voices', [])):
        raise ValueError('Published voice code collision')
    name = 'Piper LibriTTS ' + str(speaker_id).zfill(3)
    model_dir = RECIPE
    params = dict(modelDir=model_dir, noiseScale=.667, noiseScaleW=.8, lengthScale=1., threadNumber=2, provider='cpu', speakerId=str(speaker_id))
    voice = dict(code=code, name=name, locale='en-US', sampleRate=config['audio']['sample_rate'], type=0,
                 speed=1., pitch=1., volume=1., gender=-1, extendUI='[]', param=json.dumps(params, separators=(',', ':')))
    entries = {'sherpa/' + model_dir + '/' + key: value for key, value in files.items()}
    entries['config.yaml'] = yaml.safe_dump({'sherpa': [voice]}, allow_unicode=True, sort_keys=False).encode()
    entries['engines.yaml'] = b'engines:\n  - code: sherpa\n    name: Sherpa ONNX\n'
    provenance = dict(sourceUrl=asset['browser_download_url'], sourceSha256=asset['digest'].removeprefix('sha256:'),
                      sourceBytes=asset['size'], modelFingerprint=fingerprint, speakerId=speaker_id,
                      datasetLicense='CC-BY-4.0', modelAuthor='Rhasspy Piper', modelFilesModified=False,
                      sourceSpeaker=next(key for key, value in config['speaker_id_map'].items() if value == speaker_id))
    entries['sherpa/' + model_dir + '/scenevox-source.json'] = document(provenance)
    # Retain the official model repository's licence and complete attribution.
    licence_url = 'https://raw.githubusercontent.com/rhasspy/piper/master/LICENSE.md'
    licence = urllib.request.urlopen(licence_url, timeout=30).read(128 * 1024)
    if b'MIT License' not in licence or b'Permission is hereby granted' not in licence:
        raise ValueError('Upstream model licence changed')
    entries['sherpa/' + model_dir + '/model-license.txt'] = licence
    entries['sherpa/' + model_dir + '/license-sources.json'] = document(dict(modelRepository='https://huggingface.co/rhasspy/piper-voices',
              modelLicenseMetadata='https://huggingface.co/rhasspy/piper-voices/blob/main/README.md',
              licenseText=licence_url, dataset='https://www.openslr.org/60/'))
    entries['sherpa/' + model_dir + '/dataset-attribution.txt'] = b'LibriTTS, Heiga Zen et al., 2019. CC BY 4.0. https://www.openslr.org/60/\nhttps://creativecommons.org/licenses/by/4.0/\n'
    package = folder / (code + '-v1.zip')
    print(json.dumps(dict(phase='write-new-speaker-package', speakerId=speaker_id)), flush=True)
    write_zip(package, entries)
    candidate = dict(id='sherpa-' + code, name=name, engine='Sherpa ONNX', locale='en-US', language='English (United States)',
                     gender=-1, roleType='', model=RECIPE, modelFingerprint=fingerprint, speakerId=speaker_id,
                     modelSpeakerCount=config['num_speakers'], sourceSpeaker=provenance['sourceSpeaker'], sampleRate=config['audio']['sample_rate'],
                     originalPrecision='fp32', package=str(package), bytes=package.stat().st_size, sha256=file_digest(package),
                     voices=[dict(group='sherpa', code=code, type=0)], nativeVerified=False, previewAvailable=False,
                     baselineCatalogueSha256=digest(document(catalogue)), publishedPackageCount=len(catalogue['items']),
                     existingSherpaPackages=len(packages), existingModelVoiceIdentities=len(existing),
                     unlistedModelSpeakers=len(available), source=provenance)
    atomic(folder / 'candidate.json', document(candidate))
    atomic(folder / 'cases.json', document([dict(voice=name, text='A quiet breeze moves through the trees. There is time to enjoy the journey and see what lies ahead.')]))
    atomic(folder / 'native-input.json', document(dict(code=code, group='sherpa', voice=name, id='sherpa_' + code,
                                                     archive=package.name, sha256=candidate['sha256'], sampleRate=candidate['sampleRate'])))
    print(json.dumps({k: candidate[k] for k in ['id', 'speakerId', 'modelSpeakerCount', 'unlistedModelSpeakers', 'bytes', 'sha256', 'nativeVerified']}))


def validate_native_preview(raw, report, model_rate):
    # The public Android TTS service may upsample the original model to its
    # configured output rate. Verify the real WAV/report rate without treating
    # the model's own sample rate as the service's output rate.
    output_rate = report.get('sampleRate')
    if type(output_rate) is not int or not model_rate <= output_rate <= 192000:
        raise ValueError('Invalid actual native preview output rate')
    with wave.open(io.BytesIO(raw)) as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1 or audio.getframerate() != output_rate or audio.getnframes() < output_rate:
            raise ValueError('Invalid actual native preview')
    return output_rate


def finalize(folder, report_path, release_tag):
    item = json.loads((folder / 'candidate.json').read_bytes())
    package = Path(item['package'])
    if package.stat().st_size != item['bytes'] or file_digest(package) != item['sha256']:
        raise ValueError('Candidate package changed after native verification')
    report = json.loads(report_path.read_bytes())
    if not report.get('passed') or not report.get('actualNativeVoicePcmVerified') or report.get('packageSha256') != item['sha256']:
        raise ValueError('Missing native importer/synthesis acceptance for this package')
    if item.get('requiresSharedModel') and not (report.get('sharedModelReuseVerified') and report.get('fullImportVerified')):
        raise ValueError('Shared model full/delta acceptance is incomplete')
    quality = report.get('qualityAssessment', {})
    if not quality.get('passed') or quality.get('previewSha256') != report.get('sha256'):
        raise ValueError('Native preview audio QA has not passed')
    sample = Path(report['hostFile']); raw = sample.read_bytes()
    if digest(raw) != report['sha256']:
        raise ValueError('Native preview changed')
    output_rate = validate_native_preview(raw, report, item['sampleRate'])
    if quality.get('sampleRate') != output_rate:
        raise ValueError('Audio QA output rate differs from the verified preview')
    if not re.fullmatch(r'scenevox-resources-v[0-9]+', release_tag):
        raise ValueError('Invalid resource release tag')
    preview = folder / (item['id'] + '-preview.wav'); atomic(preview, raw)
    base = 'https://github.com/iamnevillechen-max/smartvoice-android-releases/releases/download/' + release_tag + '/'
    entry = {key: item[key] for key in ['id', 'name', 'engine', 'locale', 'language', 'voices']}
    entry.update(description='Offline English voice. Original full-precision model, 22.05 kHz.',
                 descriptionZh='离线英语声音，原始全精度模型，22.05 kHz。', descriptionHant='離線英語聲音，原始全精度模型，22.05 kHz。',
                 packageUrl=base + package.name, bytes=item['bytes'], sha256=item['sha256'],
                 previewUrl=base + preview.name, previewBytes=len(raw), previewSha256=digest(raw))
    if item.get('requiresSharedModel'):
        full=Path(item['fullModelPackage'])
        if full.stat().st_size!=item['fullModelBytes'] or file_digest(full)!=item['fullModelSha256']:raise ValueError('Full model package changed')
        dependency=dict(item['sharedModel'])
        dependency['configuration']=dict(url=base+package.name,bytes=item['bytes'],sha256=item['sha256'])
        entry.update(packageUrl=base+full.name,bytes=item['fullModelBytes'],sha256=item['fullModelSha256'],sharedModel=dependency)
    catalogue = json.loads(CATALOGUE.read_bytes())
    if item['baselineCatalogueSha256'] != digest(document(catalogue)) or any(v['id'] == item['id'] for v in catalogue['items']):
        raise ValueError('Published catalogue changed; repeat dedup before finalization')
    atomic(folder / 'increment.json', document(dict(schemaVersion=1, items=[entry])))
    atomic(folder / 'catalogue.pending.json', document(dict(schemaVersion=1, items=catalogue['items'] + [entry])))
    item.update(nativeVerified=True, previewAvailable=True, readyForPublication=True, publicationBlocker=None,
                verifiedApkSha256=report['installedTargetApkSha256'], previewSampleRate=output_rate,
                nativeReport=str(report_path.resolve()), humanListeningVerified=False, published=False)
    atomic(folder / 'candidate.json',document(item))
    print(json.dumps(dict(candidateVoices=1, cataloguePackages=len(catalogue['items']) + 1, nativeVerified=True, published=False)))


def configuration_only(folder):
    """Prepare a small overlay for hosts with the exact shared model already installed.

    The catalogue retains a complete first-install package and sends this delta
    only when the App verifies every required installed runtime file.
    """
    candidate_path = folder / 'candidate.json'
    candidate = json.loads(candidate_path.read_bytes())
    full = Path(candidate.get('fullModelPackage', candidate['package']))
    if file_digest(full)!=candidate.get('fullModelSha256',candidate['sha256']):
        raise ValueError('Prepared full package changed')
    prefix = 'sherpa/' + candidate['model'] + '/'
    with zipfile.ZipFile(full) as archive:
        models = [name for name in archive.namelist() if name.startswith(prefix) and name.endswith('.onnx')]
        if len(models) != 1: raise ValueError('Shared model layout')
        runtime=[]
        for name in sorted(archive.namelist()):
            if not name.startswith(prefix) or name.endswith('/'):continue
            relative=name[len(prefix):]
            if not (relative.endswith(('.onnx','.onnx.json')) or relative=='tokens.txt' or relative.startswith('espeak-ng-data/')):continue
            value=hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
            if archive.getinfo(name).file_size:runtime.append(dict(path=name,bytes=archive.getinfo(name).file_size,sha256=value.hexdigest()))
        model=next(file for file in runtime if file['path']==models[0])
        required = dict(directory=candidate['model'], file=models[0][len(prefix):], sha256=model['sha256'],
                        bytes=archive.getinfo(models[0]).file_size, fingerprint=candidate['modelFingerprint'])
        entries = {name: archive.read(name) for name in ['config.yaml', 'engines.yaml']}
    shared=dict(id=candidate['modelFingerprint'],files=runtime)
    entries['required-model.json'] = document(shared)
    package = folder / (candidate['id'] + '-configuration-v1.zip'); write_zip(package, entries)
    candidate.update(fullModelPackage=str(full), fullModelBytes=full.stat().st_size,fullModelSha256=file_digest(full), package=str(package),
                     bytes=package.stat().st_size, sha256=file_digest(package), requiresSharedModel=required,
                     sharedModel=shared,readyForPublication=False, publicationBlocker='Full/delta native acceptance pending')
    atomic(candidate_path, document(candidate))
    native = json.loads((folder / 'native-input.json').read_bytes())
    native.update(archive=package.name, sha256=candidate['sha256'], requiredModel=required,
                  fullArchive=full.name,fullSha256=candidate['fullModelSha256'],fullBytes=candidate['fullModelBytes'])
    atomic(folder / 'native-input.json', document(native))
    entry={key:candidate[key]for key in ['id','name','engine','locale','language','voices']}
    dependency=dict(shared);dependency['configuration']=dict(url='https://resource-fixture.invalid/configuration.zip',bytes=candidate['bytes'],sha256=candidate['sha256'])
    entry.update(packageUrl='https://resource-fixture.invalid/full.zip',bytes=candidate['fullModelBytes'],sha256=candidate['fullModelSha256'],sharedModel=dependency)
    atomic(folder/'native-catalogue.json',document(dict(schemaVersion=1,items=[entry])))
    print(json.dumps(dict(configurationBytes=package.stat().st_size, reusedModelBytes=required['bytes'], published=False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['discover', 'prepare', 'configuration-only', 'finalize'])
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--download', action='store_true', help='Fetch only the reviewed official model recipe')
    parser.add_argument('--speaker', type=int)
    parser.add_argument('--native-report', type=Path)
    parser.add_argument('--release-tag')
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    if args.command == 'configuration-only':
        configuration_only(args.output); return
    if args.command == 'finalize':
        if not args.native_report or not args.release_tag:
            parser.error('finalize requires --native-report and --release-tag')
        finalize(args.output, args.native_report, args.release_tag); return
    release = load_release(args.output, args.refresh)
    index = discover(release); atomic(args.output / 'inventory.json', document(index))
    if args.command == 'prepare':
        if not args.archive and not args.download:
            parser.error('prepare requires --archive or --download')
        archive = args.archive or download_reviewed_archive(args.output, release)
        prepare(args.output, archive, release, json.loads(CATALOGUE.read_bytes()), args.speaker)
    else:
        print(json.dumps(dict(assets=len(index['assets']), supportedRecipes=sum(r['state'].startswith('supported') for r in index['assets']), published=False)))


if __name__ == '__main__':
    main()
