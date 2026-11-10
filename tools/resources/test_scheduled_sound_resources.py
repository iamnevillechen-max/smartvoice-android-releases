#!/usr/bin/env python3
"""Orchestration regression tests; mocked publication never contacts GitHub."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import sound_fingerprint_index as index
import scheduled_sound_resources as scheduler
import publish_scheduled_sounds as publisher
import verify_sound_resource_import as android_import


def document(value): return (json.dumps(value, sort_keys=True) + '\n').encode()


def make_pack(folder, ident, tag='scenevox-resources-v1000'):
    audio = (ident + '-unit-test-audio').encode()
    manifest = {'schemaVersion': 1, 'id': ident, 'sounds': [{'id': ident, 'file': 'audio.wav', 'sha256': index.sha(audio)}]}
    original = index.sha(('original-' + ident).encode())
    archive = folder / (ident + '.zip')
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('pack.json', document(manifest)); z.writestr('SOURCE.json', document({'recordings': [{'originalSha256': original}]})); z.writestr('audio.wav', audio)
    preview = folder / (ident + '.wav'); preview.write_bytes(audio)
    base = 'https://github.com/' + publisher.REPO + '/releases/download/' + tag + '/'
    return {'id': ident, 'name': ident, 'engine': 'Foley', 'language': 'und', 'packageUrl': base + archive.name,
            'bytes': archive.stat().st_size, 'sha256': index.sha(archive.read_bytes()),
            'previewUrl': base + preview.name, 'previewBytes': len(audio), 'previewSha256': index.sha(audio)}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name); self.packs = self.root / 'packs'; self.packs.mkdir()
        self.old = make_pack(self.packs, 'old'); self.new = make_pack(self.packs, 'new')
        self.baseline = document({'schemaVersion': 1, 'items': [self.old]})
        self.old_index = index.build(self.baseline, self.packs)
        self.pending = document({'schemaVersion': 1, 'items': [self.old, self.new]})
        self.next_index = index.build(self.pending, self.packs, self.baseline, self.old_index)
        self.increment = document({'schemaVersion': 1, 'items': [self.new]})
        (self.root / 'increment.json').write_bytes(self.increment); (self.root / 'catalogue.pending.json').write_bytes(self.pending)
        (self.root / 'fingerprints.pending.json').write_bytes(self.next_index)
        qa = {'passed': True, 'items': [{'passed': True, 'kind': 'recording', 'sha256': self.new['previewSha256']}]}
        (self.root / 'audio-qa.json').write_bytes(document(qa))
        self.app = document({'packageName': 'com.scenevox.tts', 'versionCode': 103, 'sha256': 'a' * 64})
        self.report = {'addedPacks': 1, 'releaseTag': 'scenevox-resources-v1000', 'baselineSha256': index.sha(self.baseline),
                       'baselineIndexSha256': index.sha(self.old_index), 'incrementSha256': index.sha(self.increment)}
        (self.root / 'schedule-report.json').write_bytes(document(self.report))
        self.attestation = {'version': 1, 'passed': True, 'packageName': 'com.scenevox.tts', 'apkVersionCode': 103,
                            'apkSha256': 'a' * 64, 'incrementSha256': index.sha(self.increment),
                            'audioQaSha256': index.sha(document(qa)), 'verifiedPackIds': ['new'],
                            'testedProductionApk': True, 'publishedApk': True}
        (self.root / 'android-attestation.json').write_bytes(document(self.attestation))
    def tearDown(self): self.temporary.cleanup()
    def plan(self): return publisher.validate_candidate(self.root, self.baseline, self.old_index, self.app)


class IndexTests(Fixture):
    def test_exact_catalogue_binding(self):
        self.assertEqual(['old'], index.load(self.old_index, self.baseline)['packIds'])
        with self.assertRaises(ValueError): index.load(self.old_index, self.baseline + b' ')

    def test_increment_never_reads_old_archives(self):
        (self.packs / 'old.zip').unlink()
        self.assertEqual(self.next_index, index.build(self.pending, self.packs, self.baseline, self.old_index))

    def test_edited_old_item_rejected(self):
        changed = json.loads(self.pending); changed['items'][0]['name'] = 'changed'
        with self.assertRaises(ValueError): index.build(document(changed), self.packs, self.baseline, self.old_index)

    def test_index_needs_complete_id_set(self):
        altered = json.loads(self.old_index); altered['packIds'] = []
        with self.assertRaises(ValueError): index.load(document(altered), self.baseline)

    def test_asset_tamper_rejected(self):
        (self.packs / 'new.zip').write_bytes(b'changed')
        with self.assertRaises(ValueError): index.build(self.pending, self.packs, self.baseline, self.old_index)


class GateTests(Fixture):
    def test_exact_verified_plan_only_two_resource_paths(self):
        self.assertEqual({publisher.FEED, publisher.INDEX}, set(self.plan()['contents']))

    def test_missing_attestation_rejected(self):
        (self.root / 'android-attestation.json').unlink()
        with self.assertRaises(FileNotFoundError): self.plan()

    def test_candidate_apk_cannot_authorize_publish(self):
        for key, bad in [('publishedApk', False), ('testedProductionApk', False), ('passed', False),
                         ('apkSha256', 'b' * 64), ('incrementSha256', 'c' * 64), ('verifiedPackIds', [])]:
            (self.root / 'android-attestation.json').write_bytes(document(dict(self.attestation, **{key: bad})))
            with self.assertRaises(ValueError): self.plan()

    def test_current_published_apk_must_support_contexts(self):
        self.app = document({'packageName': 'com.scenevox.tts', 'versionCode': 102, 'sha256': 'a' * 64})
        with self.assertRaises(ValueError): self.plan()

    def test_failure_and_stale_baseline_rejected(self):
        with self.assertRaises(ValueError): publisher.validate_candidate(self.root, self.baseline + b' ', self.old_index, self.app)
        value = json.loads((self.root / 'audio-qa.json').read_bytes()); value['passed'] = False
        (self.root / 'audio-qa.json').write_bytes(document(value))
        with self.assertRaises(ValueError): self.plan()

    def test_audio_qa_must_cover_increment_pcm(self):
        qa = {'passed': True, 'items': [{'passed': True, 'kind': 'recording', 'sha256': 'f' * 64}]}
        (self.root / 'audio-qa.json').write_bytes(document(qa))
        (self.root / 'android-attestation.json').write_bytes(document(dict(self.attestation, audioQaSha256=index.sha(document(qa)))))
        with self.assertRaises(ValueError): self.plan()

    def test_no_change_never_calls_remote(self):
        (self.root / 'schedule-report.json').write_bytes(document({'addedPacks': 0}))
        self.assertEqual('no-change', publisher.publish(self.root, object())['state'])

    def test_idempotent_publish(self):
        class Remote:
            def call(self, path): return {'object': {'sha': 'head'}}
            def content(remote, path, ref): return self.pending if path == publisher.FEED else self.next_index
        self.assertEqual('already-published', publisher.publish(self.root, Remote())['state'])

    def test_remote_change_after_upload_stops_before_publish(self):
        fixture = self
        class Remote:
            def __init__(self): self.reads = 0; self.commands = []
            def call(self, path, body=None, method=None):
                self.reads += 1
                return {'object': {'sha': 'head' if self.reads == 1 else 'other-commit'}}
            def content(self, path, ref):
                return {publisher.FEED: fixture.baseline, publisher.INDEX: fixture.old_index, 'scenevox-update.json': fixture.app}[path]
            def command(self, args):
                self.commands.append(args)
                if args[:2] != ['release', 'view']: raise AssertionError('Unexpected mutation')
                assets = [{'name': name, 'size': size, 'digest': 'sha256:' + digest} for name, size, digest in [
                    ('new.zip', fixture.new['bytes'], fixture.new['sha256']),
                    ('new.wav', fixture.new['previewBytes'], fixture.new['previewSha256'])]]
                return json.dumps({'isDraft': True, 'assets': assets})
        remote = Remote()
        with self.assertRaisesRegex(ValueError, 'Repository changed'): publisher.publish(self.root, remote)
        self.assertTrue(all(command[:2] == ['release', 'view'] for command in remote.commands))

    def test_catalogue_context_cannot_differ_from_pack(self):
        changed = copy.deepcopy(self.new); changed['contexts'] = {'version': 1, 'rules': []}
        inc = document({'schemaVersion': 1, 'items': [changed]})
        pending = document({'schemaVersion': 1, 'items': [self.old, changed]})
        (self.root / 'increment.json').write_bytes(inc)
        (self.root / 'catalogue.pending.json').write_bytes(pending)
        (self.root / 'fingerprints.pending.json').write_bytes(index.build(pending, self.packs, self.baseline, self.old_index))
        (self.root / 'schedule-report.json').write_bytes(document(dict(self.report, incrementSha256=index.sha(inc))))
        (self.root / 'android-attestation.json').write_bytes(document(dict(self.attestation, incrementSha256=index.sha(inc))))
        with self.assertRaisesRegex(ValueError, 'contexts differ'): self.plan()

    def test_publish_atomic_resource_commit_and_no_update_mutation(self):
        fixture = self
        class Remote:
            def __init__(self): self.assets = {}; self.entries = []; self.bodies = {}; self.ref = 'head'; self.release = False
            def content(self, path, ref):
                if ref == 'head': return {publisher.FEED: fixture.baseline, publisher.INDEX: fixture.old_index, 'scenevox-update.json': fixture.app}[path]
                return {publisher.FEED: fixture.pending, publisher.INDEX: fixture.next_index}[path]
            def call(self, path, body=None, method=None):
                if path == 'git/ref/heads/main': return {'object': {'sha': self.ref}}
                if path == 'git/commits/head': return {'tree': {'sha': 'oldtree'}}
                if path == 'git/blobs': return {'sha': index.sha(body['content'].encode())}
                if path == 'git/trees': self.entries = body['tree']; return {'sha': 'newtree'}
                if path == 'git/commits': self.commit = body; return {'sha': 'newcommit'}
                if path == 'git/refs/heads/main':
                    fixture.assertEqual('PATCH', method); fixture.assertIs(body['force'], False); self.ref = body['sha']; return {}
                raise AssertionError(path)
            def command(self, args):
                if args[:2] == ['release', 'view']:
                    if not self.release: raise subprocess.CalledProcessError(1, [], stderr='release not found')
                    return json.dumps({'isDraft': True, 'assets': list(self.assets.values())})
                if args[:2] == ['release', 'create']: self.release = True; return ''
                if args[:2] == ['release', 'upload']:
                    path = Path(args[3]); self.assets[path.name] = {'name': path.name, 'size': path.stat().st_size, 'digest': 'sha256:' + index.sha(path.read_bytes())}; return ''
                if args[:2] == ['release', 'edit']: return ''
                raise AssertionError(args)
        remote = Remote(); result = publisher.publish(self.root, remote)
        self.assertTrue(result['published']); self.assertEqual(['head'], remote.commit['parents'])
        self.assertEqual({publisher.FEED, publisher.INDEX}, {x['path'] for x in remote.entries})


class SchedulerTests(unittest.TestCase):
    def test_android_output_plain_and_raw_formats(self):
        value = {'passed': True, 'checks': 67}
        self.assertEqual(value, android_import.parse_android_result(json.dumps(value) + '\n'))
        self.assertEqual(value, android_import.parse_android_result('INSTRUMENTATION_RESULT: stream=' + json.dumps(value) + '\nINSTRUMENTATION_CODE: -1\n'))
        with self.assertRaises(ValueError): android_import.parse_android_result('INSTRUMENTATION_FAILED')

    def test_profile_not_republished_as_weekly_alternate(self):
        self.assertFalse(scheduler.profile_available({'id': 'snow-step'}, ['source-snow-step-1234']))
        self.assertTrue(scheduler.profile_available({'id': 'snow-step'}, ['rain']))

    def test_tag_is_deterministic_and_input_order_independent(self):
        a = {'profile': {'id': 'a'}, 'clip': {'sha256': 'a' * 64}}; b = {'profile': {'id': 'b'}, 'clip': {'sha256': 'b' * 64}}
        self.assertEqual(scheduler.release_tag([a, b]), scheduler.release_tag([b, a]))

    def test_download_budget_and_request_limit(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(scheduler.sources.Client, 'get', side_effect=lambda url, maximum, *args: b'1234'[:maximum]) as fetch:
            client = scheduler.BudgetClient(Path(folder), 6, 2)
            client.get('https://kenney.nl/test'); client.get('https://kenney.nl/test')
            self.assertEqual(2, fetch.call_args[0][1])
            with self.assertRaises(ValueError): client.get('https://kenney.nl/test')

    def test_failed_download_still_consumes_reserved_budget(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(scheduler.sources.Client, 'get', side_effect=ValueError('too large')) as fetch:
            client = scheduler.BudgetClient(Path(folder), 6, 20)
            with self.assertRaises(ValueError): client.get('https://kenney.nl/test')
            with self.assertRaises(ValueError): client.get('https://kenney.nl/test')
            self.assertEqual(1, fetch.call_count)

    def test_unknown_sources_stay_outside_download_queue(self):
        self.assertFalse(scheduler.eligible({'id': 'commons:1', 'title': 'Unknown room audio.ogg'}))
        self.assertTrue(scheduler.eligible({'id': 'kenney:rpg-audio', 'title': 'RPG Audio'}))


if __name__ == '__main__': unittest.main()
