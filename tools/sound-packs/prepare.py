#!/usr/bin/env python3
"""Build the explicitly curated, named CC0 recordings; never import an entire source folder.
Build dependencies: numpy2.0.2, scipy1.13.1, soundfile0.13.1. No runtime dependency.
"""
from pathlib import Path
import argparse,hashlib,io,json,struct,zipfile
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly,fftconvolve
ROOT=Path(__file__).resolve().parents[2]
RATE=22050
SOURCES={
 'kenney':dict(file='kenney-rpg.zip',url='https://kenney.nl/assets/rpg-audio',author='Kenney',license='CC0-1.0'),
 'hael':dict(file='footsteps.zip',url='https://opengameart.org/content/footsteps-leather-cloth-armor',author='HaelDB',license='CC0-1.0'),
 'fire':dict(file='fire-1.wav',url='https://opengameart.org/content/fire-crackling',author='AntumDeluge',license='CC0-1.0'),
 'heels':dict(file='owlstorm-heels.mp3',url='https://freesound.org/people/OwlStorm/sounds/151208/',author='Ashe Kirk / Owlish Media (OwlStorm)',license='CC0-1.0',formatNote='Public HQ MP3 preview, not the original WAV master'),
 'rain':dict(file='cluckfox-rain.zip',url='https://cluckfox.itch.io/rain-and-thunder',author='CluckFox',license='CC0-1.0'),
}
SOURCES.update(json.loads(Path(__file__).with_name('freesound-sources.json').read_text()))
def sha(data):return hashlib.sha256(data).hexdigest()
def read_source(folder,key,member=None):
 source=folder/SOURCES[key]['file']
 data=zipfile.ZipFile(source).read(member) if member else source.read_bytes()
 audio,rate=sf.read(io.BytesIO(data),dtype='float64');audio=audio.mean(axis=1) if audio.ndim==2 else audio
 return audio,rate,dict(source=key,originalPath=member or source.name,originalBytes=len(data),originalSha256=sha(data),sourceSampleRate=rate)
def wav(audio):
 stream=io.BytesIO();sf.write(stream,audio,RATE,format='WAV',subtype='PCM_16');return stream.getvalue()
def excerpt(folder,key,member=None,start=0,seconds=6,sequence=None):
 audio,rate,record=read_source(folder,key,member)
 audio=audio[round(start*rate):round((start+seconds)*rate)]
 gcd=__import__('math').gcd(RATE,rate);audio=resample_poly(audio,RATE//gcd,rate//gcd,window=('kaiser',8.6))
 if sequence:
  parts=[audio]
  for additional in sequence:
   clip,sr,unused=read_source(folder,key,additional);clip=resample_poly(clip,RATE//__import__('math').gcd(RATE,sr),sr//__import__('math').gcd(RATE,sr),window=('kaiser',8.6))
   parts.extend([np.zeros(round(.34*RATE)),clip])
  audio=np.concatenate(parts)
 if not 0<len(audio)<=8*RATE or not np.isfinite(audio).all():raise ValueError('Recording bounds')
 peak=float(np.max(np.abs(audio)));rms=float(np.sqrt(np.mean(audio**2)))
 if peak<.003 or rms<.0003:raise ValueError('Unusable or silent recording')
 # Level the selected recording before PCM16 conversion; no clipped lossy-decoder overshoots.
 audio=audio*(.78/peak)
 edge=min(64,len(audio)//8);audio[:edge]*=np.linspace(0,1,edge);audio[-edge:]*=np.linspace(1,0,edge)
 record.update(startSeconds=start,selectedSeconds=round(len(audio)/RATE,6),sourcePeak=peak,sourceRms=rms,
               conversion='Mono PCM16 22050Hz, Kaiser8.6 anti-alias resampling, peak0.78, 64-frame click edges')
 return audio,record

def spatial(audio,position,distance,volume,attack,release):
 data=(ROOT/'overlays/assets/scenevox/hrtf.bin').read_bytes()
 if data[:4]!=b'SVHR':raise ValueError('HRTF')
 filters=np.frombuffer(data[20:],dtype='<f4').reshape(9,64,2)[position]
 x=audio*32768;stereo=np.column_stack([fftconvolve(x,filters[:,ear]) for ear in range(2)])
 gain=min(1,29000/max(1,float(np.max(np.abs(stereo)))))/max(1,distance)
 travel=round(distance/343*RATE);tail=RATE//20;output=np.zeros((len(stereo)+travel+tail,2))
 output[travel:travel+len(stereo)]=stereo
 for reflection in range(1,4):
  for ear in range(2):
   delay=int(RATE*(.009+reflection*.011))+(reflection*3 if ear==0 else 0)
   count=min(len(stereo),len(output)-travel-delay)
   output[travel+delay:travel+delay+count,ear]+=stereo[:count,1-ear]*(.06/reflection)*min(1,distance/3)
 duration=len(output)/RATE;t=np.arange(len(output))/RATE
 attack=min(duration*.35,attack/1000);release=min(duration*.45,release/1000)
 e=np.minimum(1,np.minimum(t/attack if attack else 1,(duration-t)/release if release else 1));e=np.clip(e,0,1);e=e*e*(3-2*e)
 return np.clip(output*gain*volume*.35/32768*e[:,None],-1,1)

def main():
 p=argparse.ArgumentParser();p.add_argument('--sources',type=Path,default=ROOT/'tmp/sfx-research');p.add_argument('--output',type=Path,default=ROOT/'dist/sound-resources');p.add_argument('--release-tag',default='scenevox-resources-v2');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
 published_path=ROOT/'distribution/scenevox-sounds-v2.json';published={x['id']:x for x in json.loads(published_path.read_text())['items']} if published_path.exists() else {}
 proof_path=ROOT/'verification/scenevox-sound-curation.json';previous_proof={x['id']:x for x in json.loads(proof_path.read_text())['items']} if proof_path.exists() else {}
 packs=[]
 def pack(id,en,cn,tw,category,description,zh,hant,sounds):packs.append(dict(id=id,name=en,nameZh=cn,nameHant=tw,category=category,description=description,descriptionZh=zh,descriptionHant=hant,sounds=sounds))
 def sound(id,en,cn,tw,keywords,key,member=None,start=0,seconds=6,sequence=None,position=1,distance=1.5,volume=.7,attack=30,release=150):
  audio,record=excerpt(a.sources,key,member,start,seconds,sequence)
  data=wav(audio);meta=dict(id=id,name=en,nameZh=cn,nameHant=tw,file='audio/'+id+'.wav',sha256=sha(data),keywords=keywords,position=position,distance=distance,volume=volume,attackMs=attack,releaseMs=release)
  record['additionalSequenceFiles']=sequence or [];return meta,data,record,audio
 definitions=[]
 for file in ['curation.json','curation-expanded.json','curation-contextual.json','curation-narrative.json','curation-opening.json','curation-notifications.json','curation-common-scenes.json','curation-scene-gaps.json']:definitions.extend(json.loads(Path(__file__).with_name(file).read_text()))
 for definition in definitions:
  if definition['id'] in published:
   # Every previously catalogued release is immutable, regardless of release tag.
   packs.append(dict(**definition,immutable=True));continue
  recordings=[sound(**entry) for entry in definition.pop('sounds')]
  packs.append(dict(**definition,sounds=recordings))
 entries=[];proof=[];seen=set()
 for package in packs:
  if package.get('immutable'):
   existing=published[package['id']];path=a.output/(package['id']+'-v1.zip');sample=a.output/(package['id']+'-preview.wav')
   if sha(path.read_bytes())!=existing['sha256'] or sha(sample.read_bytes())!=existing['previewSha256']:raise ValueError('Published package must remain byte-identical: '+package['id'])
   with zipfile.ZipFile(path) as archive:
    if archive.testzip():raise ValueError('Published pack CRC: '+package['id'])
    for recording in json.loads(archive.read('pack.json'))['sounds']:
     if recording['sha256'] in seen:raise ValueError('Duplicate curated recording')
     if sha(archive.read(recording['file']))!=recording['sha256']:raise ValueError('Published recording hash: '+recording['id'])
     seen.add(recording['sha256'])
   entries.append(existing);proof.append(previous_proof[package['id']]);continue
  manifest={key:package[key] for key in ['id','name','nameZh','nameHant']};manifest.update(schemaVersion=1,license='CC0-1.0',sounds=[entry[0] for entry in package['sounds']])
  source_records=[];preview=[]
  for meta,data,record,audio in package['sounds']:
   if meta['sha256'] in seen:raise ValueError('Duplicate curated recording')
   seen.add(meta['sha256']);source_records.append(dict(**record,outputFile=meta['file'],outputBytes=len(data),outputSha256=meta['sha256']))
   clip=spatial(audio,meta['position'],meta['distance'],meta['volume'],meta['attackMs'],meta['releaseMs'])
   preview.extend([clip,np.zeros((round(.25*RATE),2))])
  sourcekeys={r['source'] for r in source_records}
  sources={key:{**SOURCES[key],'downloadSha256':sha((a.sources/SOURCES[key]['file']).read_bytes())} for key in sorted(sourcekeys)}
  attribution=dict(sources=sources,recordings=source_records,license='CC0-1.0',preparation='Explicit curated selection, excerpts and local spatial defaults; original authors retain source credit')
  path=a.output/(package['id']+'-v1.zip')
  existing=published.get(package['id'])
  license_text='Selected recordings: CC0 1.0 Universal.\nhttps://creativecommons.org/publicdomain/zero/1.0/\nOriginal creators and recording paths are listed in SOURCE.json.\n'
  with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
   content=[('pack.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode()),('LICENSE.txt',license_text.encode()),('SOURCE.json',json.dumps(attribution,ensure_ascii=False,indent=2).encode())]+[(meta['file'],data) for meta,data,record,audio in package['sounds']]
   for name,data in content:
    info=zipfile.ZipInfo(name,(2026,11,3,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,data)
  with zipfile.ZipFile(path) as z:
   if z.testzip():raise ValueError('Pack CRC')
  sample=a.output/(package['id']+'-preview.wav');sf.write(sample,np.concatenate(preview),RATE,subtype='PCM_16')
  base='https://github.com/iamnevillechen-max/smartvoice-android-releases/releases/download/'+a.release_tag+'/'
  entry={key:package[key] for key in ['id','name','nameZh','nameHant','description','descriptionZh','descriptionHant']}
  entry.update(engine=package['category'],language='und',packageUrl=base+path.name,bytes=path.stat().st_size,sha256=sha(path.read_bytes()),previewUrl=base+sample.name,previewBytes=sample.stat().st_size,previewSha256=sha(sample.read_bytes()))
  if existing and existing['sha256']==entry['sha256'] and existing['previewSha256']==entry['previewSha256']:entry=existing
  if entry['previewBytes']>5*1024**2:raise ValueError('Preview exceeds mobile cap')
  entries.append(entry);proof.append(dict(id=package['id'],recordings=len(source_records),path=str(path),sha256=entry['sha256'],previewPath=str(sample),sources=attribution))
 (ROOT/'distribution/scenevox-sounds-v2.json').write_text(json.dumps(dict(schemaVersion=1,items=entries),ensure_ascii=False,indent=2)+'\n')
 (ROOT/'verification/scenevox-sound-curation.json').write_text(json.dumps(dict(packCount=len(entries),recordingCount=len(seen),items=proof,excludedSources=[dict(name='GarageBand / Apple Loops',reason='No repackaging as standalone sound-effect/sample packs',source='https://support.apple.com/en-ae/102034'),dict(name='Sonniss GameAudioGDC',reason='No redistribution as sound effects or asset packs under current terms',source='https://sonniss.com/gdc-bundle-license/')]),ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(dict(packs=len(entries),recordings=len(seen),bytes=sum(x['bytes'] for x in entries),previews=sum(x['previewBytes'] for x in entries))))
if __name__=='__main__':main()
