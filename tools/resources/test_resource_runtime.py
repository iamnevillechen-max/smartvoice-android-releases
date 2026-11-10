"""Runtime deployment regressions: no real service, signing, AVD or network."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import deploy_resource_runtime as deploy
import resource_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'; self.home.mkdir()
        self.patch = patch.object(Path, 'home', return_value=self.home); self.patch.start()
        self.source = self.home / 'Desktop/project'; self.source.mkdir(parents=True)
        files = ['tools/resources/consume_sound_resource_artifact.py', 'tools/resources/complete_sound_resource_run.py',
                 'tools/resources/resource_runtime.py', 'tools/resources/verify_sound_resource_import.py',
                 'tools/resources/ScheduledSoundImportInstrumentation.java', 'tools/android_validation_lock.py']
        for name in files:
            target = self.source / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text('# reviewed ' + name)
        manifest = self.source / deploy.MANIFEST; manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(deploy.document(dict(version=1, repository='iamnevillechen-max/smartvoice-android-releases',
            files=[dict(local=p, remote=p) for p in files])))
        image = self.home / 'Library/Android/sdk/system-images/android-35/default/arm64-v8a'; image.mkdir(parents=True)
        for name in ['system.img', 'vendor.img', 'kernel-ranchu']: (image / name).write_bytes(b'fixture')
        self.helper = self.base / 'helper.apk'; self.helper.write_bytes(b'signed fixture')
        self.metadata = self.base / 'metadata.json'; self.metadata.write_text('{}')
        self.output = self.base / 'review'

    def tearDown(self): self.patch.stop(); self.temp.cleanup()
    def prepare(self):
        return deploy.prepare(self.source, self.output, self.helper, self.metadata, deploy.digest(self.metadata))
    def installed(self):
        self.prepare(); deploy.install(self.source, self.output); return runtime.runtime_root()

    def test_prepare_is_review_only(self):
        result = self.prepare()
        self.assertFalse(result['enabled']); self.assertFalse(runtime.runtime_root().exists())
        self.assertFalse((self.source / '.toolchain/resource-runtime.json').exists())
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        self.assertEqual(result['sourceFiles'], 7)

    def test_install_only_pinned_files_no_secrets_or_old_avd(self):
        (self.source / 'secret.keystore').write_text('not copied')
        root = self.installed()
        self.assertFalse((root / 'secret.keystore').exists())
        self.assertFalse(list(root.rglob('*.img'))); self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        config = runtime.configuration(root)
        self.assertEqual(config['avdName'], 'scenevox_resources_api35')
        self.assertEqual((root / '.toolchain/resource-runtime.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads((self.source / '.toolchain/resource-runtime.json').read_bytes())), {'schemaVersion', 'sharedLockPath'})

    def test_launchagent_and_avd_use_only_library(self):
        self.prepare()
        plist = plistlib.loads((self.output / (deploy.LABEL + '.plist')).read_bytes())
        self.assertEqual(plist['ProgramArguments'][0], '/usr/bin/python3')
        self.assertFalse(plist['RunAtLoad']); self.assertNotIn('Desktop', json.dumps(plist))
        for path in (self.output / 'runtime/.toolchain/avd').rglob('*.ini'):
            self.assertNotIn('Desktop', path.read_text())
        self.assertIn('scenevox_resources_api35', json.dumps(deploy.avd_files(runtime.runtime_root()), default=str))

    def test_payload_tamper_rejected(self):
        self.prepare(); (self.output / 'runtime/tools/android_validation_lock.py').write_text('changed')
        with self.assertRaises(ValueError): deploy.install(self.source, self.output)
        self.assertFalse(runtime.runtime_root().exists())

    def test_source_changed_after_review_rejected(self):
        self.prepare(); (self.source / 'tools/android_validation_lock.py').write_text('changed')
        with self.assertRaises(ValueError): deploy.install(self.source, self.output)

    def test_symlink_payload_rejected(self):
        self.prepare(); path = self.output / 'runtime/tools/android_validation_lock.py'
        path.unlink(); path.symlink_to(self.source / 'tools/android_validation_lock.py')
        with self.assertRaises(ValueError): deploy.install(self.source, self.output)

    def test_runtime_rejects_workspace_root(self):
        with self.assertRaises(ValueError): runtime.configuration(self.source)

    def test_runtime_requires_private_fixed_config(self):
        root = self.installed(); path = root / '.toolchain/resource-runtime.json'; original = path.read_bytes()
        for key, value in [('avdHome', str(self.source)), ('helperApk', str(self.helper)), ('avdName', 'other'), ('schemaVersion', True), ('extra', 1)]:
            data = json.loads(original); data[key] = value; deploy.write(path, deploy.document(data))
            with self.subTest(key=key), self.assertRaises(ValueError): runtime.configuration(root)
        deploy.write(path, original, 0o644)
        with self.assertRaises(ValueError): runtime.configuration(root)

    def test_helper_metadata_tamper_rejected(self):
        root = self.installed(); (root / '.toolchain/resource-helper/helper-provenance.json').write_text('{"changed":true}')
        with self.assertRaises(ValueError): runtime.configuration(root)

    def test_symlink_runtime_rejected(self):
        root = self.installed(); metadata = root / '.toolchain/resource-helper/helper-provenance.json'
        metadata.unlink(); metadata.symlink_to(self.metadata)
        with self.assertRaises(ValueError): runtime.configuration(root)

    def test_deployment_busy_old_or_new_lock_changes_no_config(self):
        self.prepare()
        paths = [self.source / '.toolchain/locks/android-validation.lock',
                 self.home / 'Library/Application Support/SceneVox/locks/android-validation.lock']
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError): deploy.install(self.source, self.output)
                self.assertFalse((self.source / '.toolchain/resource-runtime.json').exists())

    def test_preserves_processed_checkpoint_and_existing_runtime_state(self):
        state = self.source / '.toolchain/resource-consumer/state.json'
        deploy.write(state, deploy.document(dict(processed=['123:1'], nextCheck=12345)))
        root = self.installed(); runtime_state = root / '.toolchain/resource-consumer/state.json'
        self.assertEqual(json.loads(runtime_state.read_bytes())['processed'], ['123:1'])
        deploy.write(runtime_state, deploy.document(dict(processed=['124:1'])))
        deploy.install(self.source, self.output)
        self.assertEqual(json.loads(runtime_state.read_bytes())['processed'], ['124:1'])

    def test_retains_dedicated_avd_data_on_reinstall(self):
        root = self.installed(); data = root / f'.toolchain/avd/{runtime.AVD_NAME}.avd/userdata-qemu.img'
        data.write_bytes(b'owned test data'); deploy.install(self.source, self.output)
        self.assertEqual(data.read_bytes(), b'owned test data')

    def test_helper_args_are_exact(self):
        root = self.installed(); config = runtime.configuration(root)
        self.assertEqual(runtime.helper_arguments(config), ['--helper-apk', config['helperApk'], '--helper-metadata', config['helperMetadata'], '--helper-metadata-sha256', config['helperMetadataSha256']])


if __name__ == '__main__': unittest.main()
