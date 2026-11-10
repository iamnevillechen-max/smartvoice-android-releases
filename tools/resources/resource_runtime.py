"""Fixed local resource-worker layout; no protected workspace paths at runtime."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

AVD_NAME = 'scenevox_resources_api35'
CONFIG_KEYS = {'schemaVersion', 'sharedLockPath', 'avdHome', 'avdName',
               'helperApk', 'helperMetadata', 'helperMetadataSha256'}


def runtime_root():
    return Path.home() / 'Library/Application Support/SceneVox/resource-consumer'


def configuration(root):
    root = Path(root)
    if root != runtime_root() or root.resolve() != root:
        raise ValueError('Use the installed Application Support resource worker')
    path = root / '.toolchain/resource-runtime.json'
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
        raise ValueError('Resource runtime configuration permissions or size')
    value = json.loads(path.read_bytes())
    if set(value) != CONFIG_KEYS or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise ValueError('Resource runtime configuration schema')
    expected = {
        'sharedLockPath': Path.home() / 'Library/Application Support/SceneVox/locks/android-validation.lock',
        'avdHome': root / '.toolchain/avd',
        'helperApk': root / '.toolchain/resource-helper/scheduled-sound-import-helper.apk',
        'helperMetadata': root / '.toolchain/resource-helper/helper-provenance.json',
    }
    for key, wanted in expected.items():
        if value[key] != str(wanted) or wanted.resolve() != wanted:
            raise ValueError('Resource runtime path: ' + key)
    if value['avdName'] != AVD_NAME or not re.fullmatch('[a-f0-9]{64}', value['helperMetadataSha256']):
        raise ValueError('Resource runtime AVD/helper identity')
    metadata = Path(value['helperMetadata'])
    if not metadata.is_file() or metadata.stat().st_size > 16384 or hashlib.sha256(metadata.read_bytes()).hexdigest() != value['helperMetadataSha256']:
        raise ValueError('Resource helper provenance changed')
    return value


def helper_arguments(config):
    return ['--helper-apk', config['helperApk'], '--helper-metadata', config['helperMetadata'],
            '--helper-metadata-sha256', config['helperMetadataSha256']]
