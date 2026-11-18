#!/usr/bin/env python3
"""Chinese Edge TTS and slide video; sends only authored narration to the TTS service.
Usage: python3 render_lesson.py PAPER_DIR --runtime RUNTIME_DIR [--audio-only]
No language-model calls. Resumes audio only when its text/voice digest matches.
"""
import argparse, asyncio, hashlib, json, re, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone

def stamp(): return datetime.now(timezone.utc).isoformat()
def save(path,obj):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
def srt_time(t):
    ms=round(t*1000);h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'
def captions(words,offset):
    out=[];group=[]
    for w in words:
        if group and (sum(len(x['text']) for x in group)+len(w['text'])>26 or (w['offset']-group[0]['offset'])/1e7>5):
            out.append(group);group=[]
        group.append(w)
    if group:out.append(group)
    result=[]
    for g in out:
        txt=''
        for w in g:
            if txt and txt[-1].isascii() and txt[-1].isalnum() and w['text'][0].isascii() and w['text'][0].isalnum():txt+=' '
            txt+=w['text']
        result.append((offset+g[0]['offset']/1e7,offset+(g[-1]['offset']+g[-1]['duration'])/1e7,txt))
    return result

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('paper',type=Path);ap.add_argument('--runtime',type=Path,required=True);ap.add_argument('--audio-only',action='store_true');a=ap.parse_args()
    sys.path.insert(0,str(a.runtime.resolve()))
    import edge_tts,imageio_ffmpeg
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe();p=a.paper.resolve();work=p/'media-build';work.mkdir(exist_ok=True)
    data=json.loads((p/'slides.json').read_text());status=work/'status.json'
    voice='zh-CN-XiaoxiaoNeural';rate='-5%';timeline=[];allcaps=[];offset=0
    def run(args):return subprocess.run([ffmpeg,'-hide_banner','-nostdin',*args],capture_output=True,text=True,check=True)
    def duration(file):
        r=run(['-i',str(file),'-vn','-progress','pipe:1','-f','null','-'])
        times=re.findall(r'out_time_us=(\d+)',r.stdout)
        return int(times[-1])/1e6
    try:
        for i,s in enumerate(data,1):
            audio=work/f'{i:02}.mp3';meta=work/f'{i:02}.json'
            digest=hashlib.sha256((voice+rate+s['narration']).encode()).hexdigest()
            if not(audio.exists() and meta.exists() and json.loads(meta.read_text()).get('digest')==digest):
                save(status,{'phase':'tts','slide':i,'total':len(data),'updated_at':stamp()});print(f'TTS {i}/{len(data)}',flush=True)
                words=[]
                for attempt in range(2):
                    try:
                        words=[]
                        with audio.with_suffix('.part').open('wb') as f:
                            async for chunk in edge_tts.Communicate(s['narration'],voice,rate=rate,boundary='WordBoundary').stream():
                                if chunk['type']=='audio':f.write(chunk['data'])
                                elif chunk['type']=='WordBoundary':words.append({k:chunk[k] for k in ['offset','duration','text']})
                        if not words or audio.with_suffix('.part').stat().st_size<1000:raise ValueError('missing audio or timing')
                        audio.with_suffix('.part').replace(audio);break
                    except Exception:
                        if attempt:raise
                        await asyncio.sleep(3)
                save(meta,{'digest':digest,'voice':voice,'rate':rate,'words':words})
            info=json.loads(meta.read_text());dur=duration(audio);caps=captions(info['words'],offset)
            allcaps.extend(caps);timeline.append({'slide':i,'title':s['title'],'start':offset,'duration':dur+.4,'audio_duration':dur});offset+=dur+.4
        save(p/'timeline.json',{'duration_seconds':offset,'voice':voice,'rate':rate,'slides':timeline})
        (p/'subtitles.srt').write_text('\n\n'.join(f'{i}\n{srt_time(st)} --> {srt_time(en)}\n{txt}' for i,(st,en,txt) in enumerate(allcaps,1))+'\n',encoding='utf8')
        assert all(0<=st<en<=offset for st,en,_ in allcaps)
        if a.audio_only:save(status,{'phase':'audio_complete','duration_seconds':offset,'updated_at':stamp()});return
        for row in timeline:
            i=row['slide'];segment=work/f'{i:02}.mp4';image=p/'rendered'/f'slide-{i}.png';audio=work/f'{i:02}.mp3'
            save(status,{'phase':'encoding','slide':i,'total':len(data),'updated_at':stamp()});print(f'VIDEO {i}/{len(data)}',flush=True)
            run(['-y','-loop','1','-framerate','10','-i',str(image),'-i',str(audio),'-t',str(row['duration']),'-vf','scale=1280:720,setsar=1','-c:v','libx264','-preset','veryfast','-tune','stillimage','-crf','22','-pix_fmt','yuv420p','-af','apad','-c:a','aac','-b:a','128k',str(segment)])
        concat=work/'concat.txt';concat.write_text('\n'.join(f"file '{i:02}.mp4'" for i in range(1,len(data)+1)))
        plain=work/'plain.mp4';run(['-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(plain)])
        save(status,{'phase':'subtitles','updated_at':stamp()})
        # The filter path is relative and authored by us; avoid shell interpolation.
        sub=p/'subtitles.srt'
        vf="drawbox=x=0:y=630:w=iw:h=90:color=black@0.82:t=fill,subtitles=filename='"+str(sub).replace("'","\\'")+"':force_style='Fontname=Noto Sans CJK SC,Fontsize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=1,Shadow=0,Alignment=2,MarginV=22'"
        run(['-y','-i',str(plain),'-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','22','-c:a','copy','-movflags','+faststart',str(p/'video.mp4')])
        verification=run(['-i',str(p/'video.mp4'),'-map','0:a:0','-af','volumedetect','-f','null','-'])
        (work/'audio-check.txt').write_text(verification.stderr)
        # Full decode; failures are raised, rather than silently marking ready.
        decoded=run(['-v','error','-i',str(p/'video.mp4'),'-f','null','-'])
        (work/'decode-check.txt').write_text(decoded.stderr)
        video_duration=duration(p/'video.mp4')
        save(status,{'phase':'complete','duration_seconds':video_duration,'subtitle_cues':len(allcaps),'updated_at':stamp(),'full_decode_passed':True})
        print(json.dumps({'video':str(p/'video.mp4'),'duration_seconds':video_duration,'cues':len(allcaps)},ensure_ascii=False),flush=True)
    except Exception as e:
        save(status,{'phase':'failed','error':repr(e),'updated_at':stamp()});raise

if __name__=='__main__':asyncio.run(main())
