#!/usr/bin/env python3
"""Local workbench; explicit journal saves only. No hosted model calls."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import mimetypes
from pathlib import Path
import subprocess
import sys
import urllib.parse
import radar

HERE = radar.HERE
sys.path.insert(0, str(HERE.parent))
import reading_journal as journal
PYTHON = HERE.parent / '.runtime/semantic-venv/bin/python'
PORT = 8766


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not retain users' research queries in request logs.

    def allowed(self):
        host = self.headers.get('Host','')
        allowed = {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        return host in allowed and (not origin or origin in {'http://'+x for x in allowed})

    def respond(self, status, body, kind='application/json; charset=utf-8'):
        if isinstance(body,dict):
            body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):
        if not self.allowed():return self.respond(403,{'error':'Only local same-origin requests are allowed'})
        path=urllib.parse.urlsplit(self.path).path
        if path=='/health':
            return self.respond(200,{'service':'paper-daily-workbench','version':'2.0.0-alpha.2'})
        if path in ('/','/index.html'):
            page=HERE/'index.html'
            if not page.exists():return self.respond(503,{'error':'请先构建候选页面'})
            return self.respond(200,page.read_bytes(),'text/html; charset=utf-8')
        if path=='/api/status':
            status=HERE/'data/semantic-status.json'
            return self.respond(200,{'index':json.loads(status.read_text()) if status.exists() else None})
        if path=='/reading-log':
            return self.respond(200,(HERE/'journal.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/api/journal':
            try:return self.respond(200,{'entries':journal.entries(HERE.parent)})
            except (ValueError,OSError) as exc:return self.respond(503,{'error':str(exc)})
        if path.startswith('/api/journal/request/'):
            try:
                result=journal.get_request(path.rsplit('/',1)[-1],HERE.parent)
                if result['status']=='awaiting_ai':
                    result['handoff']=journal.handoff(result['id'],HERE.parent)
                return self.respond(200,result)
            except (ValueError,OSError) as exc:return self.respond(404,{'error':str(exc)})
        if path.startswith('/library/'):
            relative=Path(urllib.parse.unquote(path[len('/library/'):]))
            target=(HERE.parent/relative).resolve()
            # Expose only published archive assets, never configs, caches, model files or source.
            permitted=(relative.as_posix() in ('index.html','reading_log.md','WORKFLOW.md','reading_profile.md','indexes/README.md') or relative.parts[:1]==('papers',))
            if not permitted or not target.is_relative_to(HERE.parent.resolve()) or any(p.startswith('.') for p in relative.parts):
                return self.respond(404,{'error':'Not found'})
            if target.is_file() and (target.suffix in ('.html','.md','.pdf','.mp4','.srt','.png','.jpg','.svg','.pptx','.bib') or target.name=='metadata.json'):
                # Stream bounded chunks; supports standard media byte range requests.
                size=target.stat().st_size;start=0;end=size-1;code=200
                byte_range=self.headers.get('Range','')
                if byte_range:
                    import re
                    m=re.fullmatch(r'bytes=(\d+)-(\d*)',byte_range)
                    if not m:return self.respond(416,{'error':'Unsupported range'})
                    start=int(m[1]);end=min(size-1,int(m[2]) if m[2] else size-1);code=206
                    if start>end:return self.respond(416,{'error':'Range outside file'})
                self.send_response(code);self.send_header('Content-Type',mimetypes.guess_type(target)[0] or 'application/octet-stream')
                self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
                self.send_header('X-Content-Type-Options','nosniff')
                if code==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                try:
                    with target.open('rb') as f:
                        f.seek(start);remaining=end-start+1
                        while remaining:
                            block=f.read(min(1024*1024,remaining))
                            if not block:break
                            self.wfile.write(block);remaining-=len(block)
                except (BrokenPipeError,ConnectionResetError):pass
                return
        return self.respond(404,{'error':'Not found'})

    def do_POST(self):
        if not self.allowed():return self.respond(403,{'error':'Only local same-origin requests are allowed'})
        if self.path in ('/api/journal/request','/api/journal/confirm','/api/journal/save-original'):
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=40000:raise ValueError('Request size invalid')
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON required')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('JSON object required')
                if self.path.endswith('/request'):
                    result=journal.create_request(data.get('paper_id'),data.get('date'),data.get('original'),data.get('revision'),HERE.parent)
                    result['handoff']=journal.handoff(result['id'],HERE.parent)
                elif self.path.endswith('/save-original'):
                    result=journal.save_original(data.get('paper_id'),data.get('date'),data.get('original'),data.get('revision'),HERE.parent)
                else:result=journal.confirm(data.get('id'),data.get('final'),data.get('revision'),HERE.parent)
                return self.respond(200,result)
            except journal.Conflict as exc:return self.respond(409,{'error':str(exc)})
            except (ValueError,TypeError) as exc:return self.respond(400,{'error':str(exc)})
            except OSError as exc:return self.respond(503,{'error':'无法保存，请保留输入并重试：'+str(exc)})
        if self.path!='/api/search':return self.respond(404,{'error':'Not found'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=10000:raise ValueError('Request size invalid')
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON required')
            data=json.loads(self.rfile.read(length))
            query=data.get('query','')
            if not isinstance(query,str) or not 0<len(query.strip())<=1000:raise ValueError('请输入1–1000个字符的问题')
            mode=data.get('mode','hybrid');scope=data.get('scope','candidates');coverage=data.get('coverage','abstracts')
            if mode not in ('hybrid','semantic','keyword') or scope not in ('candidates','archive','all','notes'):raise ValueError('Invalid mode or scope')
            if coverage not in ('abstracts','all'):raise ValueError('Invalid coverage')
            python=str(PYTHON) if PYTHON.exists() else sys.executable
            command=[python,str(HERE/'semantic.py'),'query',query,'--mode',mode,'--scope',scope,'--limit','50',
                '--source',str(data.get('source',''))[:160],'--topic',str(data.get('topic',''))[:80],'--coverage',coverage]
            result=subprocess.run(command,capture_output=True,text=True,timeout=40)
            if result.returncode:raise RuntimeError('检索进程失败；请用页面的关键词筛选继续，或检查本地运行环境')
            return self.respond(200,json.loads(result.stdout))
        except (ValueError,TypeError,json.JSONDecodeError) as exc:
            return self.respond(400,{'error':str(exc)[:200]})
        except (RuntimeError,subprocess.TimeoutExpired,OSError) as exc:
            return self.respond(503,{'error':str(exc)[:240]})


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=PORT)
    args=parser.parse_args()
    server=HTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Paper Daily: http://127.0.0.1:{args.port}',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
