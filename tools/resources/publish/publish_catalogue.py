#!/usr/bin/env python3
"""Validate and atomically publish one of the three data-only resource feeds."""
import argparse,base64,hashlib,json,re,subprocess,urllib.parse
from pathlib import Path
REPO='iamnevillechen-max/smartvoice-android-releases'
FEEDS={'voices':'multitts-resources.json','dialogues':'scenevox-dialogues.json','sounds':'scenevox-sounds-v3.json'}
def validate(raw):
    if len(raw)>1024*1024:raise ValueError('Catalogue exceeds 1 MiB')
    data=json.loads(raw);items=data['items']
    if data.get('schemaVersion')!=1 or not 0<len(items)<=1024:raise ValueError('Catalogue schema')
    ids=set()
    for item in items:
        ident=item['id']
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}',ident) or ident in ids:raise ValueError('Duplicate or invalid ID')
        ids.add(ident)
        if 'sharedModel' in item:
            model=item['sharedModel'];files=model.get('files',[]);names=set();directory=None;total=0
            if not re.fullmatch('[0-9a-f]{64}',model.get('id','')) or not 0<len(files)<=2048:raise ValueError('Shared model identity')
            for file in files:
                path=file['path'];parts=path.split('/')
                if not path.startswith('sherpa/') or '\\' in path or len(parts)<3 or any(part in {'','.','..'}for part in parts) or path in names:raise ValueError('Shared model path')
                if directory is not None and directory!=parts[1]:raise ValueError('Shared model directory')
                directory=parts[1];names.add(path)
                if type(file['bytes'])!=int or not 0<file['bytes']<=1024*1024*1024 or not re.fullmatch('[0-9a-f]{64}',file['sha256']):raise ValueError('Shared model file')
                total+=file['bytes']
            if total>1024*1024*1024:raise ValueError('Shared model capacity')
            small=model['configuration'];url=urllib.parse.urlsplit(small['url'])
            if url.scheme!='https' or not url.hostname or url.username or url.password or url.fragment or any(c.isspace()for c in small['url']):raise ValueError('Configuration URL')
            if type(small['bytes'])!=int or not 0<small['bytes']<=1024*1024 or small['bytes']>=item['bytes'] or not re.fullmatch('[0-9a-f]{64}',small['sha256']):raise ValueError('Configuration artifact')
        for name,limit in [('name',160),('engine',80),('language',80)]:
            if not isinstance(item.get(name),str) or not 0<len(item[name])<=limit:raise ValueError('Invalid '+name)
        for preview in [False,True]:
            url_key,size_key,sha_key=('previewUrl','previewBytes','previewSha256') if preview else ('packageUrl','bytes','sha256')
            if preview and not any(k in item for k in [url_key,size_key,sha_key]):continue
            url=item[url_key];parsed=urllib.parse.urlsplit(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in url):raise ValueError('HTTPS artifact URL required')
            if type(item[size_key])!=int or not 0<item[size_key]<=(5 if preview else 1024)*1024*1024:raise ValueError('Artifact size')
            if not re.fullmatch('[0-9a-fA-F]{64}',item[sha_key]):raise ValueError('Artifact SHA256')
    return data
def main():
    p=argparse.ArgumentParser();p.add_argument('--kind',choices=FEEDS,required=True);p.add_argument('--catalogue',type=Path,required=True);p.add_argument('--publish',action='store_true');a=p.parse_args()
    raw=a.catalogue.read_bytes();data=validate(raw);path=FEEDS[a.kind]
    result={'feed':path,'items':len(data['items']),'sha256':hashlib.sha256(raw).hexdigest(),'published':False}
    if a.publish:
        endpoint=f'repos/{REPO}/contents/{path}'
        existing=subprocess.run(['gh','api',endpoint],capture_output=True,text=True)
        if existing.returncode and '404' not in existing.stderr:raise RuntimeError(existing.stderr)
        body={'message':'Update '+a.kind+' resource catalogue','branch':'main','content':base64.b64encode(raw).decode()}
        if existing.returncode==0:body['sha']=json.loads(existing.stdout)['sha']
        commit=subprocess.run(['gh','api','--method','PUT',endpoint,'--input','-'],input=json.dumps(body),text=True,capture_output=True,check=True)
        result.update(published=True,commit=json.loads(commit.stdout)['commit']['sha'])
        # Read back the exact committed content; never modify the application update feed.
        verify=json.loads(subprocess.check_output(['gh','api',endpoint+'?ref='+result['commit']],text=True))
        if base64.b64decode(verify['content'])!=raw:raise RuntimeError('Published catalogue differs')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
