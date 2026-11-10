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


def digest(data):
    return hashlib.sha256(data).hexdigest()



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
    args = parser.parse_args()
    validation_lease = acquire_or_exit(ROOT)
    def run(command, timeout=120):
        result = subprocess.run([str(v) for v in command], env=dict(os.environ, JAVA_HOME=str(JAVA.parent)),
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout
    def adb(*command):
        return run([ADB, '-P5038', '-s', args.serial, *command])
    avd = adb('shell', 'getprop', 'ro.boot.qemu.avd_name').strip()
    if avd not in {'scenevox_api35_regression', 'scenevox_api37_arm64_large'}:
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
        work = Path(temporary); classes = work / 'classes'; classes.mkdir(); dex = work / 'dex'; dex.mkdir()
        manifest = work / 'AndroidManifest.xml'
        manifest.write_text(f'<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{HELPER}"><uses-sdk android:minSdkVersion="26" android:targetSdkVersion="37"/><application android:label="SceneVox Resource Verification" android:debuggable="true"/><instrumentation android:name="{RUNNER}" android:targetPackage="{PACKAGE}"/></manifest>')
        run([JAVA / 'javac', '-source', '17', '-target', '17', '-encoding', 'UTF-8', '-cp', ANDROID, '-d', classes,
             Path(__file__).with_name('ScheduledSoundImportInstrumentation.java')])
        jar = work / 'classes.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            for file in classes.rglob('*.class'): archive.write(file, file.relative_to(classes).as_posix())
        run([BT / 'd8', '--release', '--min-api', '26', '--lib', ANDROID, '--output', dex, jar])
        unsigned, aligned, signed = (work / name for name in ('unsigned.apk', 'aligned.apk', 'signed.apk'))
        run([BT / 'aapt2', 'link', '-I', ANDROID, '--manifest', manifest, '-o', unsigned])
        with zipfile.ZipFile(unsigned, 'a') as archive:
            for file in dex.glob('*.dex'): archive.write(file, file.name)
        run([BT / 'zipalign', '-f', '4', unsigned, aligned])
        metadata = OLD / '.toolchain/signing/release-signing.json'; key = json.loads(metadata.read_bytes())
        store, private = work / 'store', work / 'private'
        store.write_text(key['storePassword']); private.write_text(key['keyPassword']); store.chmod(0o600); private.chmod(0o600)
        run([BT / 'apksigner', 'sign', '--ks', metadata.parent / key['keystore'], '--ks-key-alias', key['alias'],
             '--ks-pass', 'file:' + str(store), '--key-pass', 'file:' + str(private), '--out', signed, aligned])
        adb('install', '-r', args.apk)
        target = adb('shell', 'pm', 'path', PACKAGE).strip().splitlines()[0].removeprefix('package:')
        if adb('shell', 'sha256sum', target).split()[0] != checksum:
            raise ValueError('Installed production APK differs')
        adb('install', '-r', signed)
        try:
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
                          avd=avd, api=int(adb('shell', 'getprop', 'ro.build.version.sdk').strip()))
            (args.candidate / 'android-attestation.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({'passed': True, 'packs': len(items), 'checks': report['checks'], 'publishedApk': not args.candidate_test}))
        finally:
            adb('shell', 'su', '0', 'rm', '-rf', remote)
            adb('uninstall', HELPER)


if __name__ == '__main__':
    main()
