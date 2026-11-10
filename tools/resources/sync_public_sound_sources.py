#!/usr/bin/env python3
"""Incrementally index named public sounds; stage selected originals for existing audio QA.

No credentials, app edits, catalogue publication, or bulk automatic downloads.
  python3 tools/resources/sync_public_sound_sources.py sync --source all
  python3 tools/resources/sync_public_sound_sources.py stage --id kenney:rpg-audio
The output is a source inventory, never an installable SceneVox resource feed.
"""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import http.cookiejar
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'tmp/public-sound-sources'
COMMONS = 'https://commons.wikimedia.org/w/api.php'
KENNEY = 'https://kenney.nl'
USER_AGENT = 'SceneVoxTTS-resource-index/1.0 (https://github.com/iamnevillechen-max/smartvoice-android-releases)'
MAX_SOURCE_BYTES = 128 * 1024 * 1024
AUDIO = {'.wav', '.flac', '.ogg', '.mp3', '.opus', '.aif', '.aiff'}
CATEGORIES = ('Sounds of rain', 'Sounds of wind', 'Sounds of footsteps', 'Sounds of doors', 'Sounds of water')
# Keep the current pack builder's CC0 source contract. Other Commons licences are
# reported separately rather than relabelled or silently mixed into CC0 packs.
CC0 = 'https://creativecommons.org/publicdomain/zero/1.0/'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def document(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending')
    pending.write_bytes(data)
    pending.replace(path)


def official(url, hosts):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise ValueError('Unexpected source URL')
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official(newurl, {'commons.wikimedia.org', 'upload.wikimedia.org', 'kenney.nl'})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, folder):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(OfficialRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, url, maximum=8 * 1024 * 1024, cache=True, referer=None):
        official(url, {'commons.wikimedia.org', 'upload.wikimedia.org', 'kenney.nl'})
        key = digest(url.encode())
        body, meta = self.folder / (key + '.body'), self.folder / (key + '.json')
        headers = {'User-Agent': USER_AGENT}
        if referer:
            headers['Referer'] = official(referer, {'kenney.nl'})
        saved = json.loads(meta.read_bytes()) if cache and meta.exists() else {}
        if body.exists() and saved.get('sha256') == digest(body.read_bytes()):
            for source, target in [('etag', 'If-None-Match'), ('modified', 'If-Modified-Since')]:
                if saved.get(source):
                    headers[target] = saved[source]
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                official(response.url, {'commons.wikimedia.org', 'upload.wikimedia.org', 'kenney.nl'})
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise ValueError('Source response exceeds size limit')
                if cache:
                    atomic(body, data)
                    atomic(meta, document({'sha256': digest(data), 'etag': response.headers.get('ETag'),
                                          'modified': response.headers.get('Last-Modified')}))
                return data
        except urllib.error.HTTPError as error:
            if error.code == 304 and body.exists() and saved.get('sha256') == digest(body.read_bytes()):
                data = body.read_bytes()
                if len(data) <= maximum:
                    return data
            raise


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


def plain(value):
    parser = TextOnly()
    parser.feed(str(value))
    return re.sub(r'\s+', ' ', ' '.join(parser.text)).strip()[:2000]


def commons_item(page, category):
    ident = 'commons:' + str(page['pageid'])
    info = (page.get('imageinfo') or [{}])[0]
    ext = info.get('extmetadata', {})
    field = lambda key: plain(ext.get(key, {}).get('value', ''))
    licence_url = field('LicenseUrl').replace('http://', 'https://')
    if not licence_url.startswith(CC0) or field('LicenseShortName') not in {'CC0', 'CC0 1.0'}:
        raise ValueError('outside-CC0-source-contract')
    url = official(info.get('url', ''), {'upload.wikimedia.org'})
    suffix = PurePosixPath(urllib.parse.urlsplit(url).path).suffix.lower()
    if suffix not in AUDIO or info.get('mime') not in {'application/ogg', 'audio/ogg', 'audio/wav', 'audio/x-wav', 'audio/flac', 'audio/x-flac', 'audio/mpeg', 'audio/opus', 'audio/aiff', 'audio/x-aiff'}:
        raise ValueError('not-supported-audio')
    if type(info.get('size')) is not int or not 0 < info['size'] <= MAX_SOURCE_BYTES:
        raise ValueError('source-size')
    if not re.fullmatch(r'[a-f0-9]{40}', info.get('sha1', '')) or not field('Artist'):
        raise ValueError('missing-source-identity')
    title = page['title'].removeprefix('File:')
    return dict(id=ident, source='commons', title=title, author=field('Artist'), license='CC0-1.0',
                licenseUrl=CC0, sourceUrl='https://commons.wikimedia.org/wiki/' + urllib.parse.quote(page['title'].replace(' ', '_')),
                downloadUrl=url, previewUrl=url, bytes=info['size'], sourceSha1=info['sha1'],
                sourceRevision=info.get('timestamp', ''), description=field('ImageDescription'),
                categories=[category], namedMembers=False, requiresAudioReview=True)


def commons_index(client, categories, max_pages):
    items, rejected, scans = [], [], []
    for category in categories:
        query = dict(action='query', format='json', formatversion=2, generator='categorymembers',
                     gcmtitle='Category:' + category, gcmtype='file', gcmlimit=50, gcmsort='timestamp',
                     gcmdir='desc', prop='imageinfo', iiprop='timestamp|url|size|sha1|mime|extmetadata', maxlag=5)
        complete = False
        for _ in range(max_pages):
            response = json.loads(client.get(COMMONS + '?' + urllib.parse.urlencode(query)))
            if 'error' in response:
                raise ValueError('Commons API: ' + response['error'].get('code', 'unknown'))
            for page in response.get('query', {}).get('pages', []):
                try:
                    items.append(commons_item(page, category))
                except ValueError as error:
                    rejected.append(dict(id='commons:' + str(page['pageid']), reason=str(error)))
            continuation = response.get('continue')
            if not continuation:
                complete = True
                break
            query.update(continuation)
        scans.append(dict(source='commons', category=category, complete=complete))
    return items, rejected, scans


class KenneyPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.previews, self.ids, self.heading = [], [], [], []
        self.in_heading = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h1':
            self.in_heading = True
        if tag == 'a' and attrs.get('href'):
            self.links.append(html.unescape(attrs['href']))
        if tag == 'source' and attrs.get('type', '').startswith('audio/'):
            self.previews.append(attrs.get('src', ''))
        if attrs.get('data-download-id'):
            self.ids.append(attrs['data-download-id'])

    def handle_endtag(self, tag):
        if tag == 'h1':
            self.in_heading = False

    def handle_data(self, data):
        if self.in_heading:
            self.heading.append(data.strip())


def parse_kenney(url, data):
    parser = KenneyPage()
    parser.feed(data.decode('utf-8'))
    if CC0 not in [link.replace('http://', 'https://') for link in parser.links]:
        raise ValueError('missing-CC0-source-contract')
    if not parser.previews or len(set(parser.ids)) != 1 or not re.fullmatch('[a-f0-9]+', parser.ids[0]):
        raise ValueError('missing-preview-or-download')
    slug = url.rsplit('/', 1)[1]
    preview = official(parser.previews[0], {'kenney.nl'})
    return dict(id='kenney:' + slug, source='kenney', title=parser.heading[0], author='Kenney',
                license='CC0-1.0', licenseUrl=CC0, sourceUrl=url, previewUrl=preview,
                downloadEndpoint=KENNEY + '/download-link/' + parser.ids[0],
                categories=sorted({urllib.parse.unquote(link.split('/assets/tag:', 1)[1]) for link in parser.links if '/assets/tag:' in link}),
                namedMembers=True, requiresAudioReview=True)


def kenney_index(client):
    parser = KenneyPage()
    parser.feed(client.get(KENNEY + '/assets/category:Audio').decode('utf-8'))
    links = sorted({link for link in parser.links if re.fullmatch(r'https://kenney.nl/assets/[a-z0-9-]+', link)})
    if not links or len(links) > 100:
        raise ValueError('Unexpected Kenney directory')
    items, rejected = [], []
    for url in links:
        slug = url.rsplit('/', 1)[1]
        # These are recordings/music, not synthesizable character voices or scene effects.
        if slug.startswith('voiceover-') or slug == 'music-jingles':
            continue
        try:
            items.append(parse_kenney(url, client.get(url)))
        except ValueError as error:
            rejected.append(dict(id='kenney:' + slug, reason=str(error)))
    return items, rejected, [dict(source='kenney', directory=KENNEY + '/assets/category:Audio', complete=True)]


def merge(previous, items, rejected):
    # Bounded/category scans cannot prove that an absent old file was deleted.
    rows = {item['id']: item for item in previous.get('items', [])}
    found, changes = {}, dict(added=[], changed=[], withdrawn=[])
    for item in items:
        if item['id'] in found:
            found[item['id']]['categories'] = sorted(set(found[item['id']]['categories'] + item['categories']))
        else:
            found[item['id']] = dict(item)
    for ident, item in found.items():
        item['metadataSha256'] = digest(document(item))
        old = rows.get(ident)
        if not old:
            changes['added'].append(ident)
        elif item['metadataSha256'] != old['metadataSha256']:
            changes['changed'].append(ident)
        rows[ident] = item
    for item in rejected:
        if item['id'] in rows and item['id'] not in found:
            rows.pop(item['id'])
            changes['withdrawn'].append(item['id'])
    if len(rows) > 5000:
        raise ValueError('Source inventory capacity reached')
    return dict(schemaVersion=1, kind='source-inventory', items=[rows[key] for key in sorted(rows)]), changes


def review_html(items):
    rows = []
    for item in items:
        esc = html.escape
        rows.append('<li><h2>' + esc(item['title']) + '</h2><p>' + esc(item['author']) + ' · CC0 · ' + esc(', '.join(item['categories'])) + '</p><audio controls preload="none" src="' + esc(item['previewUrl'], quote=True) + '"></audio><p><a href="' + esc(item['sourceUrl'], quote=True) + '">Source</a> · ' + esc(item['id']) + '</p></li>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Sound source review</title><h1>Sound source review</h1><ul>' + ''.join(rows) + '</ul></html>').encode()


def audio_members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 256 * 1024 * 1024:
            raise ValueError('Archive capacity')
        license_files = [i for i in infos if i.filename.lower().endswith('license.txt')]
        if not license_files or not any(b'creativecommons.org/publicdomain/zero/1.0' in archive.read(i) for i in license_files):
            raise ValueError('Archive CC0 licence missing')
        result = []
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in info.filename:
                raise ValueError('Archive path')
            if not info.is_dir() and path.suffix.lower() in AUDIO:
                raw = archive.read(info)
                result.append((info.filename, raw))
        if not result:
            raise ValueError('No named audio files')
        return result


def probe_audio(path):
    if shutil.which('ffprobe'):
        result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries', 'stream=codec_type,codec_name,sample_rate,channels:format=duration', '-of', 'json', str(path)], check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    # The repository already provisions libsndfile/numpy for audio acceptance.
    # Decode all frames rather than trusting a container header alone.
    import soundfile as sf
    import numpy as np
    with sf.SoundFile(path) as audio:
        frames = 0
        for block in audio.blocks(blocksize=65536, dtype='float32'):
            if not np.isfinite(block).all():
                raise ValueError('Invalid decoded audio')
            frames += len(block)
        return dict(streams=[dict(codec_type='audio', codec_name=audio.subtype, sample_rate=audio.samplerate,
                                  channels=audio.channels)], format=dict(duration=frames / audio.samplerate))


def stage(client, item, folder):
    if item['source'] == 'kenney':
        # Signed archive links expire. Resolve only at download time using the
        # same public endpoint linked by Kenney's own download button.
        fresh = parse_kenney(item['sourceUrl'], client.get(item['sourceUrl'], cache=False))
        link = json.loads(client.get(fresh['downloadEndpoint'], cache=False, referer=item['sourceUrl']))['url']
        data = client.get(official(link, {'kenney.nl'}), MAX_SOURCE_BYTES, cache=False)
        members = audio_members(data)
    else:
        # Recheck current page metadata before an old inventory row is downloaded.
        query = dict(action='query', format='json', formatversion=2, pageids=item['id'].split(':')[1],
                     prop='imageinfo', iiprop='timestamp|url|size|sha1|mime|extmetadata', maxlag=5)
        reply = json.loads(client.get(COMMONS + '?' + urllib.parse.urlencode(query)))
        fresh = commons_item(reply['query']['pages'][0], item['categories'][0])
        if fresh['sourceSha1'] != item['sourceSha1']:
            raise ValueError('Source changed; sync the inventory before staging')
        data = client.get(fresh['downloadUrl'], MAX_SOURCE_BYTES, cache=False)
        if len(data) != fresh['bytes'] or hashlib.sha1(data).hexdigest() != fresh['sourceSha1']:
            raise ValueError('Source audio hash/size mismatch')
        members = [(item['title'], data)]
    destination = folder / 'staged' / item['id'].replace(':', '-')
    clips, seen = [], set()
    for name, raw in members:
        checksum = digest(raw)
        if checksum in seen:
            continue
        seen.add(checksum)
        path = destination / (checksum + PurePosixPath(name).suffix.lower())
        atomic(path, raw)
        # ffprobe verifies decoded format/duration but not artistic relevance or
        # clipping. The existing sound-packs/assess_audio.py remains that gate.
        probe = probe_audio(path)
        if not probe.get('streams') or any(s.get('codec_type') != 'audio' for s in probe['streams']) or float(probe['format'].get('duration', 0)) <= 0:
            raise ValueError('Audio decode failed')
        clips.append(dict(originalPath=name, title=PurePosixPath(name).stem, file=path.name,
                          bytes=len(raw), sha256=checksum, probe=probe))
    output = dict(source=fresh, archiveSha256=digest(data), clips=clips, installable=False,
                  requiresAudioReview=True, nextStage='tools/sound-packs/assess_audio.py and curated pack preparation')
    atomic(destination / 'SOURCE.json', document(output))
    return dict(id=item['id'], stagedClips=len(clips), path=str(destination), published=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['sync', 'stage'])
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--source', choices=['commons', 'kenney', 'all'], default='all')
    parser.add_argument('--category', action='append')
    parser.add_argument('--max-pages', type=int, choices=range(1, 11), default=2)
    parser.add_argument('--id')
    args = parser.parse_args()
    client = Client(args.output / 'http-cache')
    path = args.output / 'inventory.json'
    previous = json.loads(path.read_bytes()) if path.exists() else {}
    if args.command == 'stage':
        item = next((item for item in previous.get('items', []) if item['id'] == args.id), None)
        if item is None:
            parser.error('--id must name an indexed source')
        print(json.dumps(stage(client, item, args.output), ensure_ascii=False))
        return
    items, rejected, scans = [], [], []
    for source in (['commons', 'kenney'] if args.source == 'all' else [args.source]):
        group, skip, scan = commons_index(client, args.category or CATEGORIES, args.max_pages) if source == 'commons' else kenney_index(client)
        items.extend(group)
        rejected.extend(skip)
        scans.extend(scan)
    inventory, delta = merge(previous, items, rejected)
    atomic(path, document(inventory))
    atomic(args.output / 'changes.json', document(dict(**delta, scans=scans, excluded=rejected, published=False)))
    atomic(args.output / 'review.html', review_html(inventory['items']))
    print(json.dumps(dict(items=len(inventory['items']), added=len(delta['added']), changed=len(delta['changed']),
                         withdrawn=len(delta['withdrawn']), excluded=len(rejected), scans=scans, published=False), ensure_ascii=False))


if __name__ == '__main__':
    main()
