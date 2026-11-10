#!/usr/bin/env python3
"""Split an explicit selection of reviewed, data-only channel configurations into original-format voice ZIPs."""
from pathlib import Path
import ast,hashlib,json,re,zipfile
import yaml
ROOT=Path(__file__).resolve().parents[2]
SOURCE={'mstrans':'microsoft-translate.zip','vivo':'vivo-online.zip'}
TEXT={
 'zh':'你好，现在开始试听。','en':'Hello, this is a short voice preview.',
 'it':'Ciao, questa è una breve anteprima della voce.','th':'สวัสดี นี่คือตัวอย่างเสียง',
 'vi':'Xin chào, đây là bản nghe thử giọng nói.','id':'Halo, ini adalah contoh singkat suara.',
 'ar':'مرحباً، هذه عينة صوتية قصيرة.','hi':'नमस्ते, यह आवाज़ का एक छोटा नमूना है।',
 'ru':'Здравствуйте, это короткий образец голоса.','uk':'Вітаю, це короткий зразок голосу.'
}
class MetadataLoader(yaml.SafeLoader):pass
MetadataLoader.add_constructor('tag:yaml.org,2002:org.nobody.multitts.tts.speaker.Speaker',lambda loader,node:loader.construct_mapping(node,deep=True))
def sha(data):return hashlib.sha256(data).hexdigest()
def write(path,entries):
 with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9)as z:
  parents=sorted({parent.as_posix()+'/' for name,data in entries for parent in Path(name).parents if parent.as_posix()!='.'},key=lambda name:(name.count('/'),name))
  for name in parents:
   info=zipfile.ZipInfo(name,(2026,11,3,0,0,0));info.external_attr=0o40755<<16;z.writestr(info,b'')
  for name,data in entries:
   info=zipfile.ZipInfo(name,(2026,11,3,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,data)
def main():
 selection=json.loads(Path(__file__).with_name('network-selection.json').read_text());output=ROOT/'dist/voice-resources';output.mkdir(exist_ok=True)
 items=[];batch=[];batchconfigs={};batchengines=[];batchassets=[]
 for engine,codes in selection.items():
  source=ROOT/'tmp/resource-inventory'/SOURCE[engine]
  with zipfile.ZipFile(source)as z:
   assert all(not x.filename.endswith(('.js','.so','.dex','.jar'))for x in z.infolist()),'Data-only packages required'
   config=z.read('config.yaml').decode();engines=z.read('engines.yaml');assert b'type: inner' in engines
   assert not re.search(r'!!(?!org.nobody.multitts.tts.speaker.Speaker)',config),'Unexpected YAML object tag'
   metadata=yaml.load(config,Loader=MetadataLoader)[engine];reviewed={x['code']:x for x in metadata}
   blocks=config.split('- !!org.nobody.multitts.tts.speaker.Speaker\n')[1:]
   def value(block,key):
    found=re.search(r'^  '+key+': (.*)$',block,re.M);return found.group(1).strip()if found else None
   chosen=[]
   for code in codes:
    block=next(b for b in blocks if value(b,'code')==code);name=value(block,'name');locale=value(block,'locale');gender=value(block,'gender');details=reviewed[code]
    if details.get('extendUI'):
     controls=ast.literal_eval(details['extendUI']);assert isinstance(controls,list)
     for control in controls:assert isinstance(control,dict)and set(control)<= {'code','name','value','candidate','min','max','step','type'}
     assert not re.search(r'(?:<script|javascript:|https?://|api[_-]?key|password|secret)',details['extendUI'],re.I)
    if value(block,'param') not in ['null',code]:
     # Translator SSML parameters are structured text, with no script or credentials.
     assert engine=='mstrans' and not re.search(r'(?:api[_-]?key|secret|password|javascript|<script)',block,re.I)
    entries=[('config.yaml',(engine+':\n- !!org.nobody.multitts.tts.speaker.Speaker\n'+block).encode()),('engines.yaml',engines)]
    avatar=value(block,'avatar')
    if avatar and avatar!='null':
     member=engine+'/avatar/'+avatar;assert member in z.namelist()and re.fullmatch('[A-Za-z0-9_-]+\.png',avatar);entries.append((member,z.read(member)));batchassets.append((member,z.read(member)))
    id=engine+'-'+code.lower();path=output/(id+'.zip');write(path,entries)
    items.append(dict(id=id,name=name,engine=engine,code=code,locale=locale,gender=int(gender),path=str(path.relative_to(ROOT)),bytes=path.stat().st_size,sha256=sha(path.read_bytes()),source=SOURCE[engine],sourceSha256=sha(source.read_bytes()),sourceChannel='https://t.me/MultiTTS_resource',sourceMirror='https://pan.quark.cn/s/c3b5f9152054',dataOnly=True))
    batch.append(dict(voice=name,engine=engine,text=TEXT[locale.split('-')[0]]));chosen.append('- !!org.nobody.multitts.tts.speaker.Speaker\n'+block)
   batchconfigs[engine]=''.join(chosen)
   batchengines.append(engines.decode().split('engines:\n',1)[1])
 fixture=ROOT/'tmp/resource-inventory/network-reviewed';fixture.mkdir(exist_ok=True)
 write(fixture/'curated-network-batch.zip',[('config.yaml',''.join(k+':\n'+v for k,v in batchconfigs.items()).encode()),('engines.yaml',('!!org.nobody.multitts.tts.engine.EngineConfig\nengines:\n'+''.join(batchengines)).encode())]+batchassets)
 (fixture/'curated-cases.json').write_text(json.dumps(batch,ensure_ascii=False,indent=2)+'\n');(fixture/'candidate-inventory.json').write_text(json.dumps(dict(items=items),ensure_ascii=False,indent=2)+'\n');print('Prepared',len(items),'individually named voice packages; no scripts or credentials')
if __name__=='__main__':main()
