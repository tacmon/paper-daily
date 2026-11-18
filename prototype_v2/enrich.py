#!/usr/bin/env python3
"""Bounded official publisher abstract enrichment, with cache/cooldowns."""
import argparse
from html.parser import HTMLParser
import json
import re
import time
import urllib.parse
import radar


class AbstractParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.level = 0
        self.parts = []
        self.meta = ''
        self.pdf_url = ''
        self.heading_tag, self.heading, self.await_abstract = '', [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h2', 'h3', 'h4') and not self.level:
            self.heading_tag, self.heading = tag, []
        if tag == 'meta' and attrs.get('name') in ('citation_abstract', 'description'):
            self.meta = attrs.get('content', '')
        if tag == 'meta' and attrs.get('name') == 'citation_pdf_url':
            self.pdf_url = radar.safe_url(attrs.get('content', ''))
        if self.level:
            if tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'):
                self.level += 1
        elif attrs.get('id') == 'abstract' or set(attrs.get('class', '').split()) & {'paper-abstract', 'abstract'} or (self.await_abstract and tag=='p'):
            self.level = 1
            self.await_abstract = False

    def handle_endtag(self, tag):
        if tag == self.heading_tag:
            self.await_abstract = radar.text(' '.join(self.heading)).lower() == 'abstract'
            self.heading_tag = ''
        if self.level and tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'):
            self.level -= 1

    def handle_data(self, data):
        if self.heading_tag:
            self.heading.append(data)
        if self.level:
            self.parts.append(data)

    def abstract(self):
        return radar.text(' '.join(self.parts) or self.meta)


def enrich(store, fetcher, limit=30, seconds=90):
    start = time.monotonic()
    reports = []
    rows = sorted(store.rows(), key=lambda p: (-p.get('score', 0), -int('reinforcement' in p['title'].lower()), p['title']))
    for paper in rows:
        if len(reports) >= limit or time.monotonic() - start >= seconds:
            break
        if paper.get('abstract'):
            continue
        ids = paper['identifiers']
        if ids.get('pmlr'):
            url = 'https://proceedings.mlr.press/' + ids['pmlr'] + '.html'
            source_id = 'pmlr-abstract'
        elif re.fullmatch(r'\d{4}/[a-f0-9]{32}', ids.get('iclr', '')):
            year, digest = ids['iclr'].split('/')
            url = f'https://proceedings.iclr.cc/paper_files/paper/{year}/hash/{digest}-Abstract-Conference.html'
            source_id = 'iclr-proceedings-abstract'
        elif ids.get('neurips') and re.fullmatch(r'https://proceedings\.neurips\.cc/paper_files/paper/\d{4}/hash/[a-f0-9]{32}-Abstract(?:-[\w]+)?\.html', paper.get('url','')):
            url, source_id = paper['url'], 'neurips-abstract'
        elif re.fullmatch(r'v\d+/[\w-]+', ids.get('jmlr','')):
            url, source_id = 'https://jmlr.org/papers/'+ids['jmlr']+'.html', 'jmlr-abstract'
        elif ids.get('rlj') and re.fullmatch(r'https://rlj\.org/papers/[\w-]+', paper.get('url','')):
            url, source_id = paper['url'], 'rlj-abstract'
        else:
            continue
        report = {'id': paper['id'], 'url': url}
        try:
            body, cache = fetcher.get(url)
            parser = AbstractParser()
            parser.feed(body.decode('utf-8'))
            abstract = parser.abstract()
            if len(abstract) < 80:
                raise ValueError('No substantial abstract found on official page')
            update = {'title': paper['title'], 'identifiers': ids, 'abstract': abstract}
            # Only store links actually present on the official page; a link is not a downloaded PDF.
            if parser.pdf_url and urllib.parse.urlsplit(parser.pdf_url).netloc == urllib.parse.urlsplit(url).netloc:
                update.update(pdf_url=parser.pdf_url, fulltext_status='link_unverified')
            store.upsert(update, source_id, url)
            report.update(status='ok', characters=len(abstract), **cache)
        except (RuntimeError, ValueError, UnicodeError) as exc:
            report.update(status='error', error=str(exc)[:300])
        reports.append(report)
    return reports


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--offline', action='store_true')
    p.add_argument('--limit', type=int, default=30)
    args = p.parse_args()
    config = json.loads((radar.HERE / 'sources.json').read_text())
    store = radar.Store(radar.HERE / 'data/candidates.sqlite')
    try:
        result = enrich(store, radar.Fetcher(radar.HERE / 'data/cache', config, offline=args.offline), limit=args.limit)
        radar.dump(radar.HERE / 'data/enrichment.json', result)
        print(json.dumps({'attempted': len(result), 'ok': sum(x['status']=='ok' for x in result), 'errors': [x for x in result if x['status']=='error']}, ensure_ascii=False))
    finally:
        store.close()
