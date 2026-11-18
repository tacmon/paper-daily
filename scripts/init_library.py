#!/usr/bin/env python3
"""Initialize local files without downloading papers, calling models or overwriting data."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'prototype_v2'))
import paper_library
import radar


def initialize(root=ROOT):
    root = Path(root)
    for folder in ('papers', 'days', 'feedback', 'prototype_v2/data'):
        (root/folder).mkdir(parents=True, exist_ok=True)
    for source, destination in (
        ('config.example.json', 'config.json'),
        ('reading_profile.example.md', 'reading_profile.md'),
        ('pipeline_prompt.example.md', 'pipeline_prompt.md'),
        ('prototype_v2/sources.example.json', 'prototype_v2/sources.json'),
        ('prototype_v2/research_queries.example.json', 'prototype_v2/research_queries.json'),
    ):
        if not (root/destination).exists():
            shutil.copy2(root/source, root/destination)
    journal = root/'reading_log.md'
    if not journal.exists():
        journal.write_text('# 每日一句\n\n| 日期 | 论文 | AI候选句 | 我的最终句 | 阅读笔记 |\n|---|---|---|---|---|\n', encoding='utf-8')
    paper_library.rebuild(root)
    # Build an empty usable workbench on first install; preserve existing discovery output.
    page = root/'prototype_v2/index.html'
    if not page.exists():
        store = radar.Store(root/'prototype_v2/data/candidates.sqlite')
        try:
            data = radar.snapshot(store, [])
            radar.dump(root/'prototype_v2/candidates.json', data)
            radar.build_page(data, page)
        finally:
            store.close()
    print('Local library initialized. Edit reading_profile.md before the first report.')


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    initialize()
