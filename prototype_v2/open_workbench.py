#!/usr/bin/env python3
"""Start the small local UI on demand; no scheduler and no resident model."""
import argparse
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlencode
import re
from server import PORT

HERE=Path(__file__).resolve().parent
URL=f'http://127.0.0.1:{PORT}/'
CHROME=Path(os.environ.get('PAPER_DAILY_BROWSER') or shutil.which('google-chrome-stable') or shutil.which('google-chrome') or shutil.which('chromium') or '/usr/bin/chromium')
PROFILE=Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')/'paper-daily/browser-profile'


def launch_browser(url):
    """Use Paper Daily's isolated profile, never the desktop's default browser."""
    if not CHROME.is_file():
        raise RuntimeError('未找到 Chrome/Chromium；安装浏览器或设置 PAPER_DAILY_BROWSER 为可执行文件路径。')
    PROFILE.mkdir(parents=True,exist_ok=True,mode=0o700)
    process=subprocess.Popen([str(CHROME),f'--user-data-dir={PROFILE}',
        '--no-first-run','--no-default-browser-check','--disable-sync',url],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        if process.wait(timeout=.4):raise RuntimeError('专用浏览器启动失败，请检查桌面会话。')
    except subprocess.TimeoutExpired:pass  # A new browser stays running.


def destination(scope='candidates',note=None,journal=None):
    if journal is not None:
        if journal and not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,119}',journal):raise ValueError('无效的论文编号。')
        return ensure()+'reading-log'+('?'+urlencode({'paper':journal}) if journal else '')
    if note is not None:
        target=Path(note).resolve()
        if not target.is_relative_to((HERE.parent/'papers').resolve()) or target.suffix!='.html':
            raise ValueError('只能打开论文库内的 HTML 笔记。')
        if not target.is_file():raise FileNotFoundError('笔记文件已移动，请检查论文目录。')
        return target.as_uri()  # Current-paper notes also work without the workbench server.
    if scope not in ('candidates','archive','all','notes'):raise ValueError('无效的检索范围。')
    return ensure()+('#scope='+scope if scope!='candidates' else '')


def ready():
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL+'health',timeout=1) as response:
            return json.load(response).get('service')=='paper-daily-workbench'
    except (OSError,ValueError):return False


def ensure():
    if ready():return URL
    (HERE/'data').mkdir(exist_ok=True)
    with (HERE/'data/server.log').open('a') as log:
        process=subprocess.Popen([sys.executable,str(HERE/'server.py')],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    for _ in range(30):
        if ready():return URL
        if process.poll() is not None:raise RuntimeError('工作台未能启动；检查端口8766是否被其他程序使用')
        time.sleep(.1)
    raise RuntimeError('工作台启动未及时就绪')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ensure',action='store_true')
    p.add_argument('--scope',choices=('candidates','archive','all','notes'),default='candidates')
    p.add_argument('--note',type=Path)
    p.add_argument('--journal',nargs='?',const='')
    args=p.parse_args()
    try:
        url=destination(args.scope,args.note,args.journal)
        if not args.ensure:launch_browser(url)
        print(url)
    except (OSError,RuntimeError,ValueError) as exc:
        print(str(exc),file=sys.stderr);raise SystemExit(1)
