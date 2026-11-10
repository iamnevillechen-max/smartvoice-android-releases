#!/usr/bin/env python3
"""Prepare, then optionally install a reviewed local worker. Never enables launchd."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import stat
import tempfile

from resource_runtime import runtime_root, AVD_NAME

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = 'tools/resources/publish/resource-worker-files.json'
LABEL = 'com.scenevox.resource-consumer'


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('Refusing symlink destination: ' + str(path))
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode); stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def document(value): return (json.dumps(value, indent=2) + '\n').encode()


def reviewed_files(source):
    rows = json.loads((source / MANIFEST).read_bytes())
    if rows.get('version') != 1 or rows.get('repository') != 'iamnevillechen-max/smartvoice-android-releases':
        raise ValueError('Reviewed manifest identity')
    paths = {MANIFEST}
    for row in rows['files']:
        if row['remote'] == 'scenevox-sound-fingerprints.json': continue
        value = row['local']; parts = PurePosixPath(value).parts
        if (not parts or PurePosixPath(value).is_absolute() or any(x in {'.', '..'} for x in parts)
                or '\\' in value or parts[0] not in {'tools', 'overlays'}):
            raise ValueError('Unexpected reviewed file')
        paths.add(value)
    for relative in paths:
        file = source / relative
        if file.resolve() != source / relative or not file.is_file() or file.stat().st_size > 1024 * 1024:
            raise ValueError('Reviewed source must be a bounded regular file: ' + relative)
    required = {'tools/resources/consume_sound_resource_artifact.py', 'tools/resources/complete_sound_resource_run.py',
                'tools/resources/resource_runtime.py', 'tools/resources/verify_sound_resource_import.py',
                'tools/resources/ScheduledSoundImportInstrumentation.java', 'tools/android_validation_lock.py'}
    if not required.issubset(paths): raise ValueError('Reviewed manifest omits runtime dependencies')
    return sorted(paths)


def runtime_config(destination, metadata_sha):
    return dict(schemaVersion=1,
        sharedLockPath=str(Path.home() / 'Library/Application Support/SceneVox/locks/android-validation.lock'),
        avdHome=str(destination / '.toolchain/avd'), avdName=AVD_NAME,
        helperApk=str(destination / '.toolchain/resource-helper/scheduled-sound-import-helper.apk'),
        helperMetadata=str(destination / '.toolchain/resource-helper/helper-provenance.json'),
        helperMetadataSha256=metadata_sha)


def launch_agent(destination):
    return dict(Label=LABEL, ProgramArguments=['/usr/bin/python3', str(destination / 'tools/resources/consume_sound_resource_artifact.py')],
        WorkingDirectory=str(destination), EnvironmentVariables={'PATH': str(Path.home() / '.local/bin') + ':/usr/bin:/bin:/usr/sbin:/sbin'},
        StartInterval=3600, RunAtLoad=False, ProcessType='Background',
        StandardOutPath=str(destination / '.toolchain/resource-consumer/service.log'),
        StandardErrorPath=str(destination / '.toolchain/resource-consumer/service-error.log'))


def avd_files(destination):
    image = Path.home() / 'Library/Android/sdk/system-images/android-35/default/arm64-v8a'
    if not all((image / name).is_file() for name in ['system.img', 'vendor.img', 'kernel-ranchu']):
        raise ValueError('Existing Library Android 35 image is required')
    avd_home = destination / '.toolchain/avd'
    settings = {'AvdId': AVD_NAME, 'PlayStore.enabled': 'false', 'abi.type': 'arm64-v8a',
        'avd.ini.displayname': 'SceneVox resource verification', 'avd.ini.encoding': 'UTF-8',
        'disk.dataPartition.size': '8589934592', 'fastboot.forceColdBoot': 'yes',
        'hw.accelerometer': 'yes', 'hw.arc': 'false', 'hw.audioInput': 'no', 'hw.audioOutput': 'no',
        'hw.battery': 'yes', 'hw.cpu.arch': 'arm64', 'hw.cpu.ncore': '2', 'hw.dPad': 'no', 'hw.gps': 'no',
        'hw.gpu.enabled': 'yes', 'hw.gpu.mode': 'swiftshader', 'hw.initialOrientation': 'Portrait',
        'hw.keyboard': 'yes', 'hw.lcd.density': '320', 'hw.lcd.height': '1280', 'hw.lcd.width': '720',
        'hw.mainKeys': 'no', 'hw.ramSize': '2048', 'hw.sdCard': 'no', 'image.sysdir.1': str(image) + '/',
        'runtime.network.latency': 'none', 'runtime.network.speed': 'full', 'showDeviceFrame': 'no',
        'skin.dynamic': 'yes', 'skin.name': '720x1280', 'skin.path': '_no_skin',
        'tag.display': 'Default', 'tag.id': 'default', 'target': 'android-35', 'vm.heapSize': '256'}
    return {
        f'.toolchain/avd/{AVD_NAME}.ini': ('avd.ini.encoding=UTF-8\npath=' + str(avd_home / (AVD_NAME + '.avd')) + '\ntarget=android-35\n').encode(),
        f'.toolchain/avd/{AVD_NAME}.avd/config.ini': ''.join(k + '=' + v + '\n' for k, v in settings.items()).encode(),
    }


def prepare(source, output, helper, metadata, metadata_sha):
    source, output = source.resolve(), output.resolve()
    if output.exists(): raise ValueError('Review directory already exists; use a new directory')
    if not helper.is_file() or helper.is_symlink() or helper.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Bounded prebuilt helper APK required')
    if not metadata.is_file() or metadata.is_symlink() or metadata.stat().st_size > 16384 or digest(metadata) != metadata_sha:
        raise ValueError('Prebuilt helper provenance digest mismatch')
    # The verifier performs its full target/source/signature metadata validation before installing.
    json.loads(metadata.read_bytes())
    destination = runtime_root(); payload = output / 'runtime'
    paths = reviewed_files(source)
    for relative in paths: write(payload / relative, (source / relative).read_bytes())
    write(payload / '.toolchain/resource-helper/scheduled-sound-import-helper.apk', helper.read_bytes())
    write(payload / '.toolchain/resource-helper/helper-provenance.json', metadata.read_bytes())
    config = runtime_config(destination, metadata_sha)
    write(payload / '.toolchain/resource-runtime.json', document(config))
    for path, raw in avd_files(destination).items(): write(payload / path, raw)
    write(output / (LABEL + '.plist'), plistlib.dumps(launch_agent(destination)))
    checksums = {str(p.relative_to(payload)): digest(p) for p in payload.rglob('*') if p.is_file()}
    plan = dict(schemaVersion=1, destination=str(destination), files=checksums,
        launchAgentSha256=digest(output / (LABEL + '.plist')), sourceFiles=len(paths),
        sourceBytes=sum((source / p).stat().st_size for p in paths),
        payloadBytes=sum(p.stat().st_size for p in payload.rglob('*') if p.is_file()),
        helperMetadataSha256=metadata_sha, enabled=False)
    write(output / 'deployment-plan.json', document(plan))
    return plan


@contextmanager
def migration_locks(source, destination):
    shared = Path.home() / 'Library/Application Support/SceneVox/locks/android-validation.lock'
    # Also lock both historical root-local slots, so migration cannot split a live lease.
    paths = [source / '.toolchain/locks/android-validation.lock', destination / '.toolchain/locks/android-validation.lock', shared]
    descriptors = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.resolve() != path.parent: raise ValueError('Symlink lock directory')
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid(): raise ValueError('Invalid lock owner')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(descriptors): os.close(fd)


def install(source, output):
    plan = json.loads((output / 'deployment-plan.json').read_bytes()); destination = runtime_root()
    if plan.get('schemaVersion') != 1 or plan.get('destination') != str(destination) or destination.resolve() != destination:
        raise ValueError('Deployment destination changed')
    payload = output / 'runtime'
    actual = {str(p.relative_to(payload)): digest(p) for p in payload.rglob('*') if p.is_file()}
    if actual != plan['files'] or any(p.is_symlink() for p in payload.rglob('*')):
        raise ValueError('Reviewed runtime payload changed')
    plist = output / (LABEL + '.plist')
    if digest(plist) != plan['launchAgentSha256'] or plistlib.loads(plist.read_bytes()) != launch_agent(destination):
        raise ValueError('Reviewed LaunchAgent changed')
    source_paths = reviewed_files(source)
    if any(digest(source / p) != actual.get(p) for p in source_paths):
        raise ValueError('Source changed after runtime review; prepare again')
    with migration_locks(source, destination):
        # Invalid old configuration must not silently be replaced with a second lock path.
        for root in [source, destination]:
            old = root / '.toolchain/resource-runtime.json'
            if old.exists():
                value = json.loads(old.read_bytes())
                if value.get('schemaVersion') != 1 or value.get('sharedLockPath') != str(Path.home() / 'Library/Application Support/SceneVox/locks/android-validation.lock'):
                    raise ValueError('Existing shared-lock configuration differs')
        for relative in sorted(actual):
            target = destination / relative
            if target.parent.resolve() != target.parent: raise ValueError('Symlink runtime directory')
            # Do not replace existing AVD configuration or data during a code update.
            if relative.startswith('.toolchain/avd/') and target.exists():
                if target.read_bytes() != (payload / relative).read_bytes():
                    raise ValueError('Existing dedicated AVD configuration differs')
                continue
            write(target, (payload / relative).read_bytes())
        (destination / '.toolchain/resource-consumer').mkdir(parents=True, exist_ok=True)
        # Preserve the one existing processed checkpoint, without copying logs, caches or credentials.
        old_state = source / '.toolchain/resource-consumer/state.json'
        new_state = destination / '.toolchain/resource-consumer/state.json'
        if not new_state.exists() and old_state.is_file():
            if old_state.is_symlink() or old_state.stat().st_size > 65536:
                raise ValueError('Invalid prior consumer checkpoint')
            checkpoint = json.loads(old_state.read_bytes())
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get('processed', []), list) or len(checkpoint.get('processed', [])) > 64:
                raise ValueError('Invalid prior consumer checkpoint')
            write(new_state, document(checkpoint))
        shared_config = dict(schemaVersion=1, sharedLockPath=str(Path.home() / 'Library/Application Support/SceneVox/locks/android-validation.lock'))
        write(source / '.toolchain/resource-runtime.json', document(shared_config))
        # Keep the service definition reviewable; enabling/copying it to LaunchAgents is separate.
        write(destination / (LABEL + '.plist'), plist.read_bytes())
    return dict(state='installed-not-enabled', root=str(destination), files=len(actual), payloadBytes=plan['payloadBytes'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--helper-apk', type=Path)
    parser.add_argument('--helper-metadata', type=Path)
    parser.add_argument('--helper-metadata-sha256')
    parser.add_argument('--install-reviewed', action='store_true')
    args = parser.parse_args()
    if args.install_reviewed:
        if any([args.helper_apk, args.helper_metadata, args.helper_metadata_sha256]): parser.error('Install uses only the prepared payload')
        result = install(ROOT, args.output.resolve())
    else:
        if not all([args.helper_apk, args.helper_metadata, args.helper_metadata_sha256]): parser.error('Provide the prebuilt helper and pinned metadata')
        result = prepare(ROOT, args.output, args.helper_apk, args.helper_metadata, args.helper_metadata_sha256)
    print(json.dumps(result))


if __name__ == '__main__': main()
