#!/usr/bin/env python3
"""Controlled-node continuation for a hosted-runner resource candidate."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from android_validation_lock import acquire_or_exit
from publish_scheduled_sounds import GitHub, REPO, publish
from resource_runtime import configuration, helper_arguments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--serial', choices=['emulator-5564', 'emulator-5566'], default='emulator-5566')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    config = configuration(ROOT)
    validation_lease = acquire_or_exit(ROOT)
    def finish(result):
        (args.candidate / 'publication-result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    report = json.loads((args.candidate / 'schedule-report.json').read_bytes())
    if not report['addedPacks']:
        finish({'state': 'no-change', 'published': False}); return
    github = GitHub(); head = github.call('git/ref/heads/main')['object']['sha']
    feed = github.content('scenevox-update.json', head); app = json.loads(feed)
    if app.get('packageName') != 'com.scenevox.tts' or int(app.get('versionCode', 0)) < 103:
        finish({'state': 'awaiting-contexts-capable-apk', 'published': False}); return
    url = urllib.parse.urlsplit(app['apkUrl'])
    pattern = r'/' + re.escape(REPO) + r'/releases/download/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+[.]apk)'
    match = re.fullmatch(pattern, url.path)
    if url.scheme != 'https' or url.hostname != 'github.com' or url.query or url.fragment or not match:
        raise ValueError('Unexpected published APK URL')
    if not re.fullmatch('[0-9a-f]{64}', app.get('sha256', '')) or not 0 < app.get('bytes', 0) <= 512 * 1024 * 1024:
        raise ValueError('Published APK bounds')
    with tempfile.TemporaryDirectory(prefix='scenevox-resource-node-') as folder:
        work = Path(folder); metadata = work / 'update.json'; metadata.write_bytes(feed)
        subprocess.run(['gh', 'release', 'download', match[1], '--repo', REPO, '--pattern', match[2], '--dir', str(work)], check=True, timeout=600)
        apk = work / match[2]
        if apk.stat().st_size != app['bytes'] or hashlib.sha256(apk.read_bytes()).hexdigest() != app['sha256']:
            raise ValueError('Published APK content changed')
        subprocess.run([sys.executable, str(Path(__file__).with_name('verify_sound_resource_import.py')),
            '--candidate', str(args.candidate), '--apk', str(apk), '--update-feed', str(metadata), '--serial', args.serial,
            *helper_arguments(config)], check=True, timeout=900, **validation_lease.child_options())
    result = publish(args.candidate, github) if args.publish else {'state': 'android-verified', 'published': False}
    finish(result)


if __name__ == '__main__':
    main()
