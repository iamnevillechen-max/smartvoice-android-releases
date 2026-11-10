#!/usr/bin/env python3
"""Controlled local-node hook: same-signature import test; never exports signing keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from android_validation_lock import acquire_or_exit
OLD = ROOT.parent / 'smartvoice-android'
JAVA = ROOT.parent / 'sightmate-android/.toolchain/jdk-17.0.21+9/Contents/Home/bin'
SDK = OLD / '.toolchain/android37-sdk'
BT = SDK / 'build-tools/37.0.0'
ANDROID = SDK / 'platforms/android-37.0/android.jar'
ADB = Path.home() / 'Library/Android/sdk/platform-tools/adb'
PACKAGE = 'com.scenevox.tts'
HELPER = 'com.scenevox.resourceimporttest'
RUNNER = 'org.nobody.multitts.maintained.ScheduledSoundImportInstrumentation'
SOURCE = Path(__file__).with_name('ScheduledSoundImportInstrumentation.java')
HELPER_APK_NAME = 'scheduled-sound-import-helper.apk'
HELPER_METADATA_NAME = 'helper-provenance.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()



def file_digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def helper_manifest():
    return (f'<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{HELPER}">'
            '<uses-sdk android:minSdkVersion="26" android:targetSdkVersion="37"/>'
            '<application android:label="SceneVox Resource Verification" android:debuggable="true"/>'
            f'<instrumentation android:name="{RUNNER}" android:targetPackage="{PACKAGE}"/></manifest>')


def build_command(command, timeout=120):
    """Deployment only: compiler/signing dependencies must never run on the scheduled path."""
    result = subprocess.run([str(value) for value in command],
                            env=dict(os.environ, JAVA_HOME=str(JAVA.parent)),
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not echo signing commands or the keystore configuration.
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def verified_certificates(apk):
    output = build_command([BT / 'apksigner', 'verify', '--verbose', '--print-certs', apk])
    certificates = sorted(set(re.findall(
        r'^(?:Signer #\d+ |V\d+(?:\.\d+)* Signer: )certificate SHA-256 digest: ([0-9a-fA-F]{64})$', output, re.MULTILINE)))
    if not certificates:
        raise ValueError('apksigner did not verify a signing certificate')
    return [value.lower() for value in certificates]


def build_helper(output_dir, target_apk, *, source=None):
    """Build and verify once at deployment; return runtime paths and pinned digests.

    Only the signed APK and public provenance leave the temporary build directory.
    No private signing material or JDK is copied into the runtime deployment.
    """
    output_dir, target_apk = Path(output_dir), Path(target_apk)
    source = Path(source) if source is not None else SOURCE
    source_bytes = source.read_bytes()
    target_hash = file_digest(target_apk)
    target_certificates = verified_certificates(target_apk)
    badging = build_command([BT / 'aapt2', 'dump', 'badging', target_apk])
    if not re.search(r"^package: name='" + re.escape(PACKAGE) + r"' ", badging, re.MULTILINE):
        raise ValueError('Helper target APK package mismatch')
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scenevox-build-sound-helper-') as temporary:
        work = Path(temporary)
        classes, dex = work / 'classes', work / 'dex'
        classes.mkdir(); dex.mkdir()
        manifest = work / 'AndroidManifest.xml'
        manifest.write_text(helper_manifest(), encoding='utf-8')
        source_snapshot = work / SOURCE.name
        source_snapshot.write_bytes(source_bytes)
        build_command([JAVA / 'javac', '-source', '17', '-target', '17', '-encoding', 'UTF-8',
                       '-cp', ANDROID, '-d', classes, source_snapshot])
        jar = work / 'classes.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            for file in sorted(classes.rglob('*.class')):
                archive.write(file, file.relative_to(classes).as_posix())
        build_command([BT / 'd8', '--release', '--min-api', '26', '--lib', ANDROID, '--output', dex, jar])
        unsigned, aligned, signed = (work / name for name in ('unsigned.apk', 'aligned.apk', 'signed.apk'))
        build_command([BT / 'aapt2', 'link', '-I', ANDROID, '--manifest', manifest, '-o', unsigned])
        with zipfile.ZipFile(unsigned, 'a') as archive:
            for file in sorted(dex.glob('*.dex')):
                archive.write(file, file.name)
        build_command([BT / 'zipalign', '-f', '4', unsigned, aligned])
        signing = OLD / '.toolchain/signing/release-signing.json'
        key = json.loads(signing.read_bytes())
        store, private = work / 'store', work / 'private'
        for destination, password in ((store, key['storePassword']), (private, key['keyPassword'])):
            with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                stream.write(password)
        build_command([BT / 'apksigner', 'sign', '--ks', signing.parent / key['keystore'],
                       '--ks-key-alias', key['alias'], '--ks-pass', 'file:' + str(store),
                       '--key-pass', 'file:' + str(private), '--out', signed, aligned])
        helper_certificates = verified_certificates(signed)
        if helper_certificates != target_certificates:
            raise ValueError('Helper must have the production APK signing identity')
        if source.read_bytes() != source_bytes or file_digest(target_apk) != target_hash:
            raise ValueError('Helper build input changed during deployment')
        provenance = {
            'schemaVersion': 1, 'helperPackage': HELPER, 'targetPackage': PACKAGE, 'runner': RUNNER,
            'source': {'name': SOURCE.name, 'sha256': digest(source_bytes)},
            'manifestSha256': digest(manifest.read_bytes()),
            'apk': {'name': HELPER_APK_NAME, 'sha256': file_digest(signed), 'bytes': signed.stat().st_size},
            'targetApk': {'sha256': target_hash, 'bytes': target_apk.stat().st_size},
            'signatureVerification': {
                'tool': 'apksigner verify --verbose --print-certs',
                'helperVerified': True, 'targetVerified': True,
                'helperCertificateSha256': helper_certificates,
                'targetCertificateSha256': target_certificates,
            },
        }
        metadata = work / HELPER_METADATA_NAME
        metadata.write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        signed.replace(output_dir / HELPER_APK_NAME)
        metadata.replace(output_dir / HELPER_METADATA_NAME)
    return {
        'helperApk': str(output_dir / HELPER_APK_NAME),
        'helperMetadata': str(output_dir / HELPER_METADATA_NAME),
        'helperMetadataSha256': file_digest(output_dir / HELPER_METADATA_NAME),
        'helperSha256': provenance['apk']['sha256'],
    }


def validate_helper(helper_apk, metadata, metadata_sha256, target_apk, *, source=None, helper_sha256=None):
    """Validate deployment-pinned evidence using hashes only; no signing keys or subprocesses."""
    helper_apk, metadata, target_apk = Path(helper_apk), Path(metadata), Path(target_apk)
    source = Path(source) if source is not None else SOURCE
    if not re.fullmatch('[0-9a-f]{64}', metadata_sha256 or ''):
        raise ValueError('Expected helper metadata SHA-256 is required')
    if metadata.stat().st_size > 65536:
        raise ValueError('Helper provenance is too large')
    raw = metadata.read_bytes()
    if digest(raw) != metadata_sha256:
        raise ValueError('Helper provenance changed')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('schemaVersion') != 1:
        raise ValueError('Unsupported helper provenance')
    if (value.get('helperPackage'), value.get('targetPackage'), value.get('runner')) != (HELPER, PACKAGE, RUNNER):
        raise ValueError('Helper target or runner mismatch')
    if value.get('source') != {'name': SOURCE.name, 'sha256': file_digest(source)}:
        raise ValueError('Helper source changed; rebuild at deployment')
    if value.get('manifestSha256') != digest(helper_manifest().encode('utf-8')):
        raise ValueError('Helper manifest changed; rebuild at deployment')
    expected_apk = value.get('apk', {})
    actual_apk = {'name': HELPER_APK_NAME, 'sha256': file_digest(helper_apk), 'bytes': helper_apk.stat().st_size}
    if expected_apk != actual_apk or (helper_sha256 is not None and helper_sha256 != actual_apk['sha256']):
        raise ValueError('Signed helper APK changed')
    if value.get('targetApk') != {'sha256': file_digest(target_apk), 'bytes': target_apk.stat().st_size}:
        raise ValueError('Helper was built for a different production APK')
    evidence = value.get('signatureVerification', {})
    certificates = evidence.get('helperCertificateSha256')
    if (evidence.get('tool') != 'apksigner verify --verbose --print-certs'
            or evidence.get('helperVerified') is not True or evidence.get('targetVerified') is not True
            or not isinstance(certificates, list) or not certificates
            or any(not isinstance(c, str) or not re.fullmatch('[0-9a-f]{64}', c) for c in certificates)
            or certificates != evidence.get('targetCertificateSha256')):
        raise ValueError('Missing same-signature deployment verification')
    return value


def prepare_helper(work, target_apk, *, helper_apk=None, helper_metadata=None,
                   helper_metadata_sha256=None, helper_sha256=None):
    provided = (helper_apk, helper_metadata, helper_metadata_sha256)
    if any(value is not None for value in provided):
        if not all(value is not None for value in provided):
            raise ValueError('Prebuilt helper APK, metadata and expected metadata SHA-256 are required together')
        prebuilt = True
    else:
        if helper_sha256 is not None:
            raise ValueError('Explicit helper SHA-256 requires a prebuilt helper')
        built = build_helper(Path(work) / 'built-helper', target_apk)
        helper_apk, helper_metadata = built['helperApk'], built['helperMetadata']
        helper_metadata_sha256 = built['helperMetadataSha256']
        prebuilt = False
    provenance = validate_helper(helper_apk, helper_metadata, helper_metadata_sha256, target_apk,
                                 helper_sha256=helper_sha256)
    # Install a private validated snapshot, not a mutable deployment path.
    raw = Path(helper_apk).read_bytes()
    if digest(raw) != provenance['apk']['sha256']:
        raise ValueError('Signed helper changed while staging')
    staged = Path(work) / HELPER_APK_NAME
    staged.write_bytes(raw)
    return staged, {'prebuilt': prebuilt, 'apkSha256': provenance['apk']['sha256'],
                    'metadataSha256': helper_metadata_sha256, 'sourceSha256': provenance['source']['sha256'],
                    'signerCertificateSha256': provenance['signatureVerification']['helperCertificateSha256']}


def parse_android_result(output):
    found = re.search(r'INSTRUMENTATION_RESULT: stream=(\{.*\})', output)
    value = json.loads(found.group(1) if found else output.strip())
    if not isinstance(value, dict):
        raise ValueError('No instrumented result')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--update-feed', type=Path, required=True)
    parser.add_argument('--serial', choices=['emulator-5564', 'emulator-5566'], default='emulator-5566')
    parser.add_argument('--candidate-test', action='store_true', help='Local evidence only; cannot authorize publication')
    parser.add_argument('--helper-apk', type=Path)
    parser.add_argument('--helper-metadata', type=Path)
    parser.add_argument('--helper-metadata-sha256')
    parser.add_argument('--helper-sha256')
    args = parser.parse_args()
    validation_lease = acquire_or_exit(ROOT)
    def run(command, timeout=120):
        result = subprocess.run([str(v) for v in command], env=os.environ,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout
    def adb(*command):
        return run([ADB, '-P5038', '-s', args.serial, *command])
    avd = adb('shell', 'getprop', 'ro.boot.qemu.avd_name').strip()
    if avd not in {'scenevox_api35_regression', 'scenevox_api37_arm64_large', 'scenevox_resources_api35'}:
        raise ValueError('Only the owned SceneVox resource test AVD is allowed')
    app = json.loads(args.update_feed.read_bytes())
    checksum = digest(args.apk.read_bytes())
    if app.get('packageName') != PACKAGE or int(app.get('versionCode', 0)) < 103 or checksum != app.get('sha256'):
        raise ValueError('APK must match exact contexts-capable update metadata')
    increment_raw = (args.candidate / 'increment.json').read_bytes()
    items = json.loads(increment_raw)['items']
    if not 0 < len(items) <= 24:
        raise ValueError('Candidate capacity')
    remote = '/data/media/0/Android/data/' + PACKAGE + '/files/verification/scheduled-sounds'
    with tempfile.TemporaryDirectory(prefix='scenevox-resource-verify-') as temporary:
        work = Path(temporary)
        signed, helper_evidence = prepare_helper(
            work, args.apk, helper_apk=args.helper_apk, helper_metadata=args.helper_metadata,
            helper_metadata_sha256=args.helper_metadata_sha256, helper_sha256=args.helper_sha256)
        adb('install', '-r', args.apk)
        target = adb('shell', 'pm', 'path', PACKAGE).strip().splitlines()[0].removeprefix('package:')
        if adb('shell', 'sha256sum', target).split()[0] != checksum:
            raise ValueError('Installed production APK differs')
        try:
            adb('install', '-r', signed)
            adb('shell', 'su', '0', 'mkdir', '-p', remote)
            def stage(file, name):
                staging = '/data/local/tmp/scenevox-resource-verification'
                adb('push', file, staging)
                try: adb('shell', 'su', '0', 'cp', staging, remote + '/' + name)
                finally: adb('shell', 'rm', '-f', staging)
            stage(args.candidate / 'increment.json', 'increment.json')
            for item in items:
                if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}', item['id']):
                    raise ValueError('Invalid resource identity')
                path = args.candidate / 'packs' / item['packageUrl'].rsplit('/', 1)[-1]
                data = path.read_bytes()
                if len(data) != item['bytes'] or digest(data) != item['sha256']:
                    raise ValueError('Resource archive changed')
                stage(path, item['id'] + '.zip')
            uid = re.search(r'uid:(\d+)', adb('shell', 'pm', 'list', 'packages', '-U', PACKAGE)).group(1)
            adb('shell', 'su', '0', 'chown', '-R', uid + ':' + uid, remote)
            output = run([ADB, '-P5038', '-s', args.serial, 'shell', 'am', 'instrument', '-w', '-r', HELPER + '/' + RUNNER], timeout=600)
            (args.candidate / 'android-import.txt').write_text(output)
            report = parse_android_result(output)
            if report.get('passed') is not True or report.get('actualVersionCode') != app['versionCode'] or report.get('incrementSha256') != digest(increment_raw) or report.get('verifiedPackIds') != sorted(x['id'] for x in items):
                raise ValueError('Android import did not verify the exact increment')
            report.update(version=1, packageName=PACKAGE, apkSha256=checksum, apkVersionCode=app['versionCode'],
                          audioQaSha256=digest((args.candidate / 'audio-qa.json').read_bytes()), publishedApk=not args.candidate_test,
                          helper=helper_evidence, avd=avd, api=int(adb('shell', 'getprop', 'ro.build.version.sdk').strip()))
            (args.candidate / 'android-attestation.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({'passed': True, 'packs': len(items), 'checks': report['checks'], 'publishedApk': not args.candidate_test}))
        finally:
            try:
                adb('shell', 'su', '0', 'rm', '-rf', remote)
            finally:
                adb('uninstall', HELPER)


if __name__ == '__main__':
    main()
