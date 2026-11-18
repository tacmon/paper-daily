#!/usr/bin/env python3
"""Open canonical paper notes in Obsidian; never copy or rewrite note bodies."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import paper_library as library

INDEX = '知识库/精读导航.md'


def digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


def refresh_index(root, papers=None, days=None):
    root = Path(root)
    if papers is None:
        papers, days, errors = library.load_library(root)
        if errors:
            raise ValueError('; '.join(errors))
    lines = ['按日报日期排列；链接指向每篇元数据登记的当前Markdown笔记，不复制正文。',
             '本页由程序更新。个人想法请写在批注或原笔记中；本页如被手动编辑，程序会保留并提示冲突。', '',
             '| 日期 | 中文标题与笔记 | PDF | 状态 |', '| --- | --- | --- | --- |']
    for day, record in sorted(days.items(), reverse=True):
        p = papers[record['paper_id']]
        base = root/'papers'/p['id']
        artifacts = p.get('artifacts', {})
        def link(key, label):
            relative = artifacts.get(key)
            if not relative:
                return '尚无'
            target = library.inside(base, relative)
            if not target.is_file():
                return '尚无'
            text = str(label).replace('|', ' / ').replace('[', '(').replace(']', ')').replace('\n', ' ')
            return f'[[{target.relative_to(root).as_posix()}\\|{text}]]'
        title = p.get('title_zh') or p['title']
        status = '已就绪' if library.effective_status(root, p) == 'ready' else '待完成'
        lines.append(f"| {day} | {link('notes', title)} | {link('pdf', '原文')} | {status} |")
    content = '\n'.join(lines) + '\n'
    target = root/INDEX
    state_path = root/'.obsidian/paper-daily-index.json'
    state = library.read_json(state_path) if state_path.exists() else {}
    if target.exists() and digest(target.read_text()) not in (state.get('sha256'), digest(content)):
        return {'updated': False, 'warning': '精读导航有手动修改，已保留；未覆盖笔记或批注。'}
    if target.is_symlink():
        raise ValueError('精读导航不能是软链接')
    library.atomic_text(target, content)
    library.atomic_json(state_path, {'generator': 'obsidian_bridge.py', 'sha256': digest(content)})
    return {'updated': True, 'index': INDEX}


def note_uri(item, root=ROOT):
    root = Path(root).resolve()
    relative = item['data'].get('artifacts', {}).get('notes')
    if not relative:
        raise FileNotFoundError('这一篇还没有Markdown笔记。')
    target = library.inside(item['base'], relative)
    target.relative_to(root)
    if not target.is_file() or target.suffix.lower() != '.md':
        raise FileNotFoundError('Markdown笔记不存在。')
    return 'obsidian://open?' + urlencode({'path': str(target), 'paneType': 'tab'})


def registered(root=ROOT):
    registry = Path.home()/'.config/obsidian/obsidian.json'
    try:
        data = json.loads(registry.read_text())
        return any(Path(v.get('path', '')).resolve() == Path(root).resolve() for v in data.get('vaults', {}).values())
    except (OSError, ValueError, TypeError):
        return False


def open_note(item, root=ROOT):
    uri = note_uri(item, root)
    if not registered(root):
        raise RuntimeError('请先在Obsidian“管理知识库 → 打开已有文件夹”中选择：' + str(root))
    if not shutil.which('obsidian'):
        raise RuntimeError('找不到Obsidian；浏览器阅读入口仍可使用。')
    # The URI protocol works without CLI being enabled. No shell or model calls.
    result = subprocess.run(['xdg-open', uri], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:240] or 'Obsidian URI未能启动。')
    return {'requested_uri': uri, 'note': item['data']['artifacts']['notes']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['sync', 'status', 'open'])
    parser.add_argument('--paper')
    args = parser.parse_args()
    if args.action == 'sync':
        result = refresh_index(ROOT)
    elif args.action == 'status':
        result = {'vault_root': str(ROOT), 'registered': registered(), 'app_available': bool(shutil.which('obsidian'))}
    else:
        papers, _, errors = library.load_library(ROOT)
        if errors or args.paper not in papers:
            raise ValueError('请选择有效论文ID；' + '; '.join(errors))
        result = open_note({'data': papers[args.paper], 'base': ROOT/'papers'/args.paper})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
