#!/usr/bin/env python3
"""Curate self-contained voices from the exact published MultiTTS Microsoft pack.

Vendor payload files and original per-voice parameters remain byte-for-byte unchanged.
Outputs are unpublished candidates until the original engine produces verified PCM/samples.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

EXPECTED='3ef02cfc2281fca596a91a52a5e327cbe54d12122e76bbc2496586812213ba03'
SOURCE_URL='https://github.com/samitooooo/MultiTTS/releases/download/Hello/Microsoft.23.Update.OfflineVoices.zip'
SELECTED=[('zh-CN-XiaoxiaoNeural','晓晓','zh-CN','microsoft-xiaoxiao'),('zh-CN-YunxiNeural','云希','zh-CN','microsoft-yunxi'),('en-US-AriaNeural','Aria','en-US','microsoft-aria'),('en-US-GuyNeural','Guy','en-US','microsoft-guy')]
def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):result.update(data)
    return result.hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('archive',type=Path);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--all',action='store_true');args=parser.parse_args()
    if digest(args.archive)!=EXPECTED:raise SystemExit('Source package is incomplete or not the reviewed release')
    args.output.mkdir(parents=True,exist_ok=True);items=[]
    with zipfile.ZipFile(args.archive) as source:
        if source.testzip() is not None:raise SystemExit('Source ZIP CRC failure')
        infos=source.infolist();names=[i.filename for i in infos]
        if len(names)!=len(set(names)):raise SystemExit('Duplicate original ZIP entries')
        for name in names:
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\'in name:raise SystemExit('Unsafe original ZIP entry')
        configuration=source.read('config.yaml').decode('utf-8')
        if not configuration.startswith('microsoft:\n'):raise SystemExit('Unexpected original config root')
        blocks=re.split(r'(?m)(?=^- !!org\.nobody\.multitts\.tts\.speaker\.Speaker\s*$)',configuration)[1:]
        if len(blocks)!=23:raise SystemExit('Unexpected original speaker list')
        engines=source.read('engines.yaml')
        selected=SELECTED
        if args.all:
            selected=[]
            for block in blocks:
                code=re.search(r'^  code: (.+)$',block,re.M).group(1)
                name=re.search(r'^  name: (.+)$',block,re.M).group(1)
                language=re.search(r'^  locale: (.+)$',block,re.M).group(1)
                identifier='microsoft-'+code.split('-')[-1].removesuffix('Neural').lower()
                selected.append((code,name,language,identifier))
        for code,name,language,identifier in selected:
            matches=[b for b in blocks if re.search(r'^  code: '+re.escape(code)+r'\s*$',b,re.M)]
            if len(matches)!=1:raise SystemExit('Speaker config not unique: '+code)
            config=('microsoft:\n'+matches[0]).encode('utf-8')
            prefix='microsoft/'+code+'/'
            chosen=[info for info in infos if info.filename=='microsoft/'or info.filename.startswith(prefix)]
            if not any(not info.is_dir() for info in chosen):raise SystemExit('Voice payload absent: '+code)
            out=args.output/(identifier+'-v1.zip');payload=[]
            with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as target:
                for filename,data in [('config.yaml',config),('engines.yaml',engines)]:
                    info=zipfile.ZipInfo(filename,(1981,1,1,1,1,1));info.compress_type=zipfile.ZIP_DEFLATED;target.writestr(info,data)
                for original in chosen:
                    data=source.read(original.filename);info=zipfile.ZipInfo(original.filename,(1981,1,1,1,1,1));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=original.external_attr;target.writestr(info,data)
                    if not original.is_dir():payload.append({'path':original.filename,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            with zipfile.ZipFile(out) as verify:
                if verify.testzip()is not None:raise SystemExit('Curated ZIP CRC failure')
                for entry in payload:
                    if hashlib.sha256(verify.read(entry['path'])).hexdigest()!=entry['sha256']:raise SystemExit('Vendor payload changed')
            gender=int(re.search(r'^  gender: ([01])$',matches[0],re.M).group(1))
            record={'id':identifier,'name':name,'language':language,'gender':gender,'engine':'Microsoft TTS','engineCode':'microsoft','code':code,'path':str(out),'bytes':out.stat().st_size,'sha256':digest(out),'payloadFiles':payload,'configSha256':hashlib.sha256(config).hexdigest(),'sourcePackageUrl':SOURCE_URL,'sourcePackageSha256':EXPECTED,'sourceParametersUnchanged':True,'vendorPayloadBytesUnchanged':True,'runtimeVerified':False,'previewAvailable':False,'published':False}
            items.append(record);print(identifier,record['bytes'],record['sha256'],flush=True)
    (args.output/'candidate-inventory.json').write_text(json.dumps({'schemaVersion':1,'status':'unpublished-candidates','items':items},ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
