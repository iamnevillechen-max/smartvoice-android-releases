#!/usr/bin/env python3
"""Local-only consumer of reviewed workflow data. Not installed or enabled by default."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from android_validation_lock import Lease, Busy
from publish_scheduled_sounds import GitHub, REPO
from resource_runtime import configuration, AVD_NAME

REPOSITORY_ID = 1441760437
WORKFLOW = '.github/workflows/weekly-sound-resources.yml'
MANIFEST = ROOT / 'tools/resources/publish/resource-worker-files.json'
STATE = ROOT / '.toolchain/resource-consumer/state.json'
ADB = Path.home() / 'Library/Android/sdk/platform-tools/adb'
EMULATOR = Path.home() / 'Library/Android/sdk/emulator/emulator'
AVD = AVD_NAME
SERIAL = 'emulator-5566'
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
WEEK = 7 * 24 * 3600


class Deferred(RuntimeError): pass
class Rejected(RuntimeError): pass


def sha(data): return hashlib.sha256(data).hexdigest()
def git_sha(data): return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
def artifact_file_limit(): resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE, MAX_ARCHIVE))
def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(value, indent=2) + '\n'); pending.chmod(0o600); pending.replace(path)


def valid_run(run, workflow_id):
    return (run.get('workflow_id') == workflow_id and run.get('path') == WORKFLOW and
            run.get('status') == 'completed' and run.get('conclusion') == 'success' and
            run.get('event') in {'schedule', 'workflow_dispatch'} and run.get('head_branch') == 'main' and
            run.get('repository', {}).get('id') == REPOSITORY_ID and
            run.get('head_repository', {}).get('id') == REPOSITORY_ID and
            re.fullmatch('[a-f0-9]{40}', run.get('head_sha', '')) is not None)


def reviewed_sources(api, head):
    tree = api.call('git/trees/' + head + '?recursive=1')
    if tree.get('truncated'): raise Rejected('Source tree is incomplete')
    entries = {row['path']: row for row in tree['tree']}
    manifest = json.loads(MANIFEST.read_bytes())
    if manifest['repository'] != REPO: raise Rejected('Local deployment manifest repository')
    for row in manifest['files']:
        if row['remote'] == 'scenevox-sound-fingerprints.json': continue
        actual = entries.get(row['remote'], {})
        if actual.get('type') != 'blob' or actual.get('sha') != git_sha((ROOT / row['local']).read_bytes()):
            raise Rejected('Workflow preparation source differs from reviewed local files: ' + row['remote'])


def select_artifact(api, processed):
    workflow = api.call('actions/workflows/' + WORKFLOW.rsplit('/', 1)[-1])
    if workflow.get('path') != WORKFLOW or workflow.get('state') != 'active':
        raise Deferred('Fixed resource workflow is not active')
    runs = api.call('actions/workflows/' + str(workflow['id']) + '/runs?branch=main&status=success&per_page=10')['workflow_runs']
    for run in runs:
        if not valid_run(run, workflow['id']): continue
        key = str(run['id']) + ':' + str(run.get('run_attempt', 1))
        # Never replay an older artifact after a newer successful run was handled.
        if key in processed: return None
        reviewed_sources(api, run['head_sha'])
        artifacts = api.call('actions/runs/' + str(run['id']) + '/artifacts?per_page=100')
        name = 'scenevox-sound-candidate-' + str(run['id'])
        matches = [x for x in artifacts['artifacts'] if x['name'] == name]
        if len(matches) != 1: raise Rejected('Expected one fixed-name resource artifact')
        artifact = matches[0]
        identity = artifact.get('workflow_run', {})
        if artifact.get('expired') or identity.get('id') != run['id'] or identity.get('head_sha') != run['head_sha']:
            raise Rejected('Resource artifact run identity or expiry')
        if not 0 < artifact.get('size_in_bytes', 0) <= MAX_ARCHIVE or not re.fullmatch('sha256:[a-f0-9]{64}', artifact.get('digest', '')):
            raise Rejected('Resource artifact size or digest')
        return run, artifact, key
    return None


def extract_candidate(archive, destination, expected_digest):
    if archive.stat().st_size > MAX_ARCHIVE or 'sha256:' + sha(archive.read_bytes()) != expected_digest:
        raise Rejected('Artifact ZIP digest or size mismatch')
    with zipfile.ZipFile(archive) as source:
        items = source.infolist()
        if len(items) > 4096 or sum(x.file_size for x in items) > MAX_EXPANDED:
            raise Rejected('Artifact expansion capacity')
        names = set()
        for item in items:
            parts = PurePosixPath(item.filename).parts
            mode = item.external_attr >> 16
            if (not parts or PurePosixPath(item.filename).is_absolute() or any(x in {'.', '..'} for x in parts) or
                    '\\' in item.filename or item.filename in names or stat.S_ISLNK(mode) or item.flag_bits & 1):
                raise Rejected('Unsafe artifact member')
            names.add(item.filename)
            if item.is_dir(): continue
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG}: raise Rejected('Unexpected artifact entry type')
            if parts[0] != 'candidate': continue
            if PurePosixPath(item.filename).suffix not in {'.json', '.zip', '.wav', '.log', '.txt'}:
                raise Rejected('Candidate contains non-data files')
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output, length=1024*1024)
            target.chmod(0o600)
    candidate = destination / 'candidate'
    if not (candidate / 'schedule-report.json').is_file(): raise Rejected('Candidate report missing')
    return candidate


def current_identity(api, candidate):
    head = api.call('git/ref/heads/main')['object']['sha']
    report = json.loads((candidate / 'schedule-report.json').read_bytes())
    paths = [('scenevox-sounds-v3.json', 'baselineSha256', 'catalogue.pending.json'),
             ('scenevox-sound-fingerprints.json', 'baselineIndexSha256', 'fingerprints.pending.json')]
    current = {path: api.content(path, head) for path, _, _ in paths}
    if all((candidate / pending).is_file() and current[path] == (candidate / pending).read_bytes() for path, _, pending in paths):
        return None  # Publication finished before a prior local process saved its checkpoint.
    for path, key, _ in paths:
        if sha(current[path]) != report.get(key): raise Rejected('Current resource baseline changed')
    app = json.loads(api.content('scenevox-update.json', head))
    if app.get('packageName') != 'com.scenevox.tts' or int(app.get('versionCode', 0)) < 103:
        raise Deferred('Awaiting the published contexts-capable APK')
    if not re.fullmatch('[a-f0-9]{64}', app.get('sha256', '')): raise Rejected('Published APK identity')
    return app


def output(command, timeout=30):
    return subprocess.check_output([str(x) for x in command], text=True, stderr=subprocess.STDOUT, timeout=timeout)


def port_busy(port):
    with socket.socket() as probe: return probe.connect_ex(('127.0.0.1', port)) == 0


def idle_device_server():
    # Do not start ADB merely to inspect whether another validation is using it.
    if port_busy(5038):
        lines = output([ADB, '-P5038', 'devices']).strip().splitlines()
        if any(line.strip() for line in lines[1:]): raise Deferred('Android device server is in use')
    if port_busy(5566) or port_busy(5567): raise Deferred('Reserved emulator ports are in use')


def stop_owned(process, signum=signal.SIGTERM):
    if process is None or process.poll() is not None: return
    # Each child owns a fresh process group. Never issue a serial-based kill.
    try: os.killpg(process.pid, signum)
    except ProcessLookupError: return
    try: process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=10)


def run_android(candidate, lease):
    config = configuration(ROOT)
    avd_home = Path(config['avdHome'])
    idle_device_server()
    if not (avd_home / (AVD + '.avd/config.ini')).is_file(): raise Rejected('Owned AVD configuration missing')
    env = dict(os.environ, ANDROID_AVD_HOME=str(avd_home), ANDROID_ADB_SERVER_PORT='5038',
               ANDROID_ADB_SERVER_ADDRESS='127.0.0.1', ADB_SERVER_SOCKET='tcp:127.0.0.1:5038')
    emulator = child = None
    with (candidate / 'local-node.log').open('w') as log:
        try:
            emulator = subprocess.Popen([str(EMULATOR), '-avd', AVD, '-port', '5566', '-no-window',
                '-no-audio', '-no-boot-anim', '-no-snapshot', '-gpu', 'swiftshader_indirect'], env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if emulator.poll() is not None: raise RuntimeError('Owned emulator exited during startup')
                try:
                    if output([ADB, '-P5038', '-s', SERIAL, 'shell', 'getprop', 'sys.boot_completed'], 5).strip() == '1':
                        if output([ADB, '-P5038', '-s', SERIAL, 'shell', 'getprop', 'ro.boot.qemu.avd_name']).strip() != AVD:
                            raise Rejected('Unexpected emulator identity')
                        break
                except (subprocess.SubprocessError, OSError): pass
                time.sleep(2)
            else: raise RuntimeError('Owned emulator startup timed out')
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('complete_sound_resource_run.py')),
                '--candidate', str(candidate), '--serial', SERIAL, '--publish'], stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, **lease.child_options())
            if child.wait(timeout=1800): raise RuntimeError('Local Android verification or publication failed')
            result = json.loads((candidate / 'publication-result.json').read_bytes())
            if result.get('state') not in {'published', 'already-published'}: raise Deferred(result.get('state', 'Not published'))
            return result
        finally:
            # SIGINT lets Python TemporaryDirectory/finally remove signing-password files.
            try: stop_owned(child, signal.SIGINT)
            finally: stop_owned(emulator)


def consume(api, lease, state):
    selected = select_artifact(api, state.get('processed', []))
    if selected is None: return {'state': 'no-new-artifact', 'published': False}, None
    run, artifact, key = selected
    with tempfile.TemporaryDirectory(prefix='resource-consumer-', dir=str(STATE.parent)) as folder:
        work = Path(folder); archive = work / 'artifact.zip'
        with archive.open('wb') as stream:
            subprocess.run(['gh', 'api', 'repos/' + REPO + '/actions/artifacts/' + str(artifact['id']) + '/zip'],
                           stdout=stream, stderr=subprocess.PIPE, check=True, timeout=180, preexec_fn=artifact_file_limit)
        candidate = extract_candidate(archive, work, artifact['digest'])
        report = json.loads((candidate / 'schedule-report.json').read_bytes())
        if report.get('published') is not False or report.get('state') not in {'prepared', 'no-change'}:
            raise Rejected('Artifact is not an unpublished candidate')
        if report.get('addedPacks') == 0: return {'state': 'no-change', 'published': False}, key
        app = current_identity(api, candidate)
        if app is None: return {'state': 'already-published', 'published': False}, key
        identity = {'repositoryId': REPOSITORY_ID, 'runId': run['id'], 'headSha': run['head_sha'],
                    'artifactId': artifact['id'], 'artifactDigest': artifact['digest'], 'preflightApkSha256': app['sha256']}
        atomic(candidate / 'local-consumption.json', identity)
        try:
            result = run_android(candidate, lease)
        finally:
            # Keep latest diagnostics also on failure. The folder contains only our fixed files.
            evidence = STATE.parent / 'last-acceptance'; evidence.mkdir(exist_ok=True)
            for name in ('local-consumption.json', 'android-attestation.json', 'publication-result.json', 'android-import.txt', 'local-node.log'):
                target = evidence / name
                if (candidate / name).is_file(): shutil.copyfile(candidate / name, target); target.chmod(0o600)
                elif target.exists(): target.unlink()
        return result, key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--now', action='store_true', help='Ignore the local weekly/retry due time')
    args = parser.parse_args()
    configuration(ROOT)
    def interrupted(*_): raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, interrupted)
    # This only activates when explicitly run or when the separate template is installed.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with (STATE.parent / 'consumer.lock').open('a') as consumer_lock:
        try: fcntl.flock(consumer_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'state': 'consumer-already-running', 'published': False})); return
        state = json.loads(STATE.read_bytes()) if STATE.exists() else {}
        now = time.time()
        if not args.now and now < state.get('nextCheck', 0):
            print(json.dumps({'state': 'not-due', 'published': False})); return
        try:
            with Lease(ROOT) as lease:
                idle_device_server()
                result, key = consume(GitHub(), lease, state)
                if key: state['processed'] = (state.get('processed', []) + [key])[-64:]
                state.update(nextCheck=now+WEEK, lastResult=result, checkedAt=datetime.now(timezone.utc).isoformat())
                atomic(STATE, state)
        except (Busy, Deferred) as error:
            result = {'state': 'deferred', 'reason': str(error), 'published': False}
            state.update(nextCheck=now+3600, lastResult=result); atomic(STATE, state)
        except (Rejected, subprocess.SubprocessError, OSError, ValueError, RuntimeError) as error:
            result = {'state': 'failed', 'reason': str(error)[:400], 'published': False}
            state.update(nextCheck=now+3600, lastResult=result); atomic(STATE, state)
        print(json.dumps(result))


if __name__ == '__main__': main()
