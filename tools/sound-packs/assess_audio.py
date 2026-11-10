#!/usr/bin/env python3
"""Offline asset checks and FFmpeg EBU R128 measurements; never modify audio.

Run with .toolchain/audio-qa/bin/python (numpy, imageio-ffmpeg==0.6.0).
Short effects may be below the R128 integration gate; that is reported, not
misrepresented as a quality failure or fixed by repeating/padding the signal.
"""
from pathlib import Path
import argparse
import hashlib
import io
import json
import re
import subprocess
import tempfile
import wave
import zipfile

import imageio_ffmpeg
import numpy as np


def signal(data):
    with wave.open(io.BytesIO(data)) as stream:
        assert stream.getsampwidth() == 2
        rate, channels = stream.getframerate(), stream.getnchannels()
        raw = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')
    samples = raw.astype(np.float64) / 32768
    assert raw.size and np.isfinite(samples).all()
    return dict(rate=rate, channels=channels, frames=raw.size // channels,
                seconds=raw.size / channels / rate,
                samplePeakDbfs=float(20 * np.log10(max(1e-12, np.abs(samples).max()))),
                rmsDbfs=float(20 * np.log10(max(1e-12, np.sqrt(np.mean(samples**2))))),
                clippedSamples=int(np.count_nonzero((raw == 32767) | (raw == -32768))),
                dcOffset=float(samples.mean()),
                activeFraction=float(np.mean(np.abs(samples) > .001)))


def usable(info):
    return (info['clippedSamples'] == 0 and info['rmsDbfs'] > -70
            and info['activeFraction'] > .01 and abs(info['dcOffset']) < .03)


def negative_controls():
    def wav(samples):
        out = io.BytesIO()
        with wave.open(out, 'wb') as stream:
            stream.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
            stream.writeframes(np.asarray(samples, dtype='<i2').tobytes())
        return out.getvalue()
    assert not usable(signal(wav(np.zeros(22050))))
    assert not usable(signal(wav(np.full(22050, 32767))))
    assert not usable(signal(wav(np.full(22050, 10000))))
    assert usable(signal(wav(np.sin(np.arange(22050) * .1) * 8000)))
    return ['silence rejected', 'clipping rejected', 'DC-only rejected', 'ordinary signal accepted']


def ffmpeg_metrics(exe, data, logs, identifier):
    with tempfile.TemporaryDirectory(prefix='scenevox-sound-qa-') as work:
        path = Path(work) / 'signal.wav'
        path.write_bytes(data)
        process = subprocess.run([exe, '-hide_banner', '-nostats', '-i', str(path),
                                  '-af', 'ebur128=peak=true,astats=reset=0',
                                  '-f', 'null', '-'], capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr[-1000:]
    assert 'Summary:' in process.stderr and 'True peak:' in process.stderr
    (logs / (identifier + '.log')).write_text(process.stderr)
    def last(pattern):
        found = re.findall(pattern, process.stderr)
        return float(found[-1]) if found else None
    return dict(integratedLufs=last(r'I:\s+(-?\d+(?:\.\d+)?) LUFS'),
                truePeakDbfs=last(r'Peak:\s+(-?\d+(?:\.\d+)?) dBFS'),
                loudnessRangeLu=last(r'LRA:\s+(-?\d+(?:\.\d+)?) LU'),
                gateNote='No padding or repetition; short clips may remain below R128 gate',
                rawLog=str(logs / (identifier + '.log')))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalogue', type=Path, required=True)
    p.add_argument('--folder', type=Path, required=True)
    p.add_argument('--baseline-folder', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    logs = args.output.parent / (args.output.stem + '-logs')
    logs.mkdir(parents=True, exist_ok=True)
    seen, rows = {}, []
    if args.baseline_folder:
        for path in args.baseline_folder.glob('*-v1.zip'):
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.endswith('.wav'):
                        seen[hashlib.sha256(z.read(name)).hexdigest()] = path.name + '/' + name
    for item in json.loads(args.catalogue.read_text())['items']:
        name = item['packageUrl'].rsplit('/', 1)[1]
        path = args.folder / name
        assert path.stat().st_size == item['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
            for clip in json.loads(z.read('pack.json'))['sounds']:
                data = z.read(clip['file'])
                digest = hashlib.sha256(data).hexdigest()
                assert digest == clip['sha256'] and digest not in seen, (clip['id'], seen.get(digest))
                seen[digest] = clip['id']
                info = signal(data)
                measured = ffmpeg_metrics(exe, data, logs, clip['id'])
                rows.append(dict(id=clip['id'], kind='recording', sha256=digest,
                                 passed=usable(info), **info, **measured))
        path = args.folder / item['previewUrl'].rsplit('/', 1)[1]
        data = path.read_bytes()
        assert len(data) == item['previewBytes']
        assert hashlib.sha256(data).hexdigest() == item['previewSha256']
        info = signal(data)
        rows.append(dict(id=item['id'], kind='preview', passed=usable(info), **info,
                         **ffmpeg_metrics(exe, data, logs, item['id'] + '-preview')))
    report = dict(passed=all(row['passed'] for row in rows), negativeControls=negative_controls(),
                  measurements=len(rows), items=rows,
                  ffmpegVersion=subprocess.check_output([exe, '-version'], text=True).splitlines()[0],
                  ffmpegBinarySha256=hashlib.sha256(Path(exe).read_bytes()).hexdigest(),
                  documentation='https://ffmpeg.org/ffmpeg-filters.html#ebur128-1',
                  scope='Asset integrity, PCM safety, exact duplicate rejection, objective level measurements. Not an aesthetic or real-phone listening verdict.')
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'items'}, ensure_ascii=False))
    assert report['passed'], 'Asset safety checks failed; inspect report before publishing'


if __name__ == '__main__':
    main()
