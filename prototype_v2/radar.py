#!/usr/bin/env python3
"""Bounded, cacheable paper discovery prototype. No model or publishing calls."""
from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
import email.utils
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import signal
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
LIBRARY = HERE.parent
AGENT = 'PaperDailyPrototype/0.1 (local academic reading assistant)'
TOPICS = [
    ('goal', '目标条件与能力估计', 5, [r'goal[ -]conditioned', r'goal generation', r'achievable goal', r'goal exploration']),
    ('curriculum', '自动课程与教师学生', 4, [r'curricul\w*', r'teacher[ -]student', r'task generation']),
    ('progress', '学习进展与练习价值', 5, [r'learning progress', r'competence progress']),
    ('change', '非平稳与证据失效', 4, [r'non[ -]?stationar\w*', r'concept drift', r'distribution shift']),
    ('feedback', '反馈偏差与可靠验证', 4, [r'noisy rewards?', r'biased feedback', r'reward corruption', r'imperfect feedback']),
    ('reasoning', '推理学习与可验证反馈', 3, [r'verifiable rewards?', r'process (?:supervision|reward)', r'(?:reasoning|self.evol\w*).{0,30}(?:reinforcement|language model)', r'curricul\w*.{0,30}(?:reasoning|language model)']),
    ('theory', '在线学习与动态遗憾', 3, [r'dynamic regret', r'adaptive regret', r'non.stationary bandit', r'variation budget']),
]


def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')


def dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def text(value):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))).split())


def safe_url(value):
    value = str(value or '').strip()
    p = urllib.parse.urlsplit(value)
    return value if p.scheme in ('https', 'http') and p.netloc and not p.username else ''


def norm_id(kind, value):
    value = str(value).strip()
    if kind == 'doi':
        return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value, flags=re.I).lower()
    if kind == 'arxiv':
        value = re.sub(r'^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/', '', value)
        return re.sub(r'v\d+$', '', re.sub(r'\.pdf$', '', value))
    return value


def title_key(value):
    return re.sub(r'\W+', '', unicodedata.normalize('NFKC', value).casefold())


def signals(paper):
    title, abstract = paper.get('title', '').lower(), paper.get('abstract', '').lower()
    matches, score = [], 0
    for key, label, weight, patterns in TOPICS:
        location = '标题' if any(re.search(p, title) for p in patterns) else '摘要'
        if location == '标题' or any(re.search(p, abstract) for p in patterns):
            score += weight * (2 if location == '标题' else 1)
            matches.append({'id': key, 'label': label, 'where': location})
    if matches and re.search(r'reinforcement learning', title + ' ' + abstract):
        score += 3
    return score, matches


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS papers(id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS identifiers(kind TEXT, value TEXT, paper_id TEXT REFERENCES papers(id), PRIMARY KEY(kind,value));
          CREATE TABLE IF NOT EXISTS observations(paper_id TEXT REFERENCES papers(id), source_id TEXT, url TEXT, seen_at TEXT, data TEXT, PRIMARY KEY(paper_id,source_id,url));
          CREATE TABLE IF NOT EXISTS conflicts(signature TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, at TEXT, source_id TEXT, data TEXT);
        ''')

    def close(self):
        self.db.close()

    def upsert(self, paper, source_id, url):
        ids = {k: norm_id(k, v) for k, v in paper['identifiers'].items() if v}
        if not ids or not paper.get('title'):
            raise ValueError('paper needs a title and a stable identifier')
        targets = set()
        for kind, value in ids.items():
            row = self.db.execute('SELECT paper_id FROM identifiers WHERE kind=? AND value=?', (kind, value)).fetchone()
            if row:
                targets.add(row[0])
        if len(targets) > 1:
            issue = {'identifiers': ids, 'records': sorted(targets), 'reason': '标识连接了多条记录，需核对版本，未自动合并'}
            signature = hashlib.sha256(json.dumps(issue, sort_keys=True).encode()).hexdigest()
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO conflicts VALUES(?,?)', (signature, json.dumps(issue, ensure_ascii=False)))
            return None, False
        pid = next(iter(targets)) if targets else 'p-' + hashlib.sha256(json.dumps(sorted(ids.items())).encode()).hexdigest()[:18]
        row = self.db.execute('SELECT data FROM papers WHERE id=?', (pid,)).fetchone()
        old = json.loads(row[0]) if row else {}
        merged = dict(old)
        # Keep archived publication metadata authoritative; retain every source observation.
        for key, value in paper.items():
            if value and (not old.get('archived') or paper.get('archived') or key not in old):
                merged[key] = value
        merged['identifiers'] = dict(old.get('identifiers', {}), **ids)
        merged['first_seen_at'] = old.get('first_seen_at') or now()
        merged['last_seen_at'] = now()
        merged['discovery_batch'] = old.get('discovery_batch') or paper.get('discovery_batch', 'legacy')
        if len(old.get('abstract', '')) > len(merged.get('abstract', '')):
            merged['abstract'] = old['abstract']
        if old.get('pdf_url') and paper.get('fulltext_status') == 'not_fetched' and old.get('fulltext_status'):
            merged['fulltext_status'] = old['fulltext_status']
        with self.db:
            self.db.execute('INSERT INTO papers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (pid, json.dumps(merged, ensure_ascii=False)))
            for kind, value in ids.items():
                self.db.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?)', (kind, value, pid))
            self.db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?)', (pid, source_id, url, now(), json.dumps(paper, ensure_ascii=False)))
        return pid, row is None

    def rows(self):
        result = []
        for row in self.db.execute('SELECT id,data FROM papers ORDER BY id'):
            d = json.loads(row['data'])
            d.setdefault('authors', [])
            d.setdefault('abstract', '')
            d['id'] = row['id']
            d['sources'] = [dict(x) for x in self.db.execute('SELECT source_id,url,seen_at FROM observations WHERE paper_id=?', (row['id'],))]
            d['score'], d['topics'] = signals(d)
            result.append(d)
        # Similar titles are a review warning, never an automatic identity assertion.
        groups = defaultdict(list)
        for d in result:
            groups[title_key(d['title'])].append(d)
        for group in groups.values():
            if len(group) > 1:
                for d in group:
                    d['possible_duplicate'] = True
        return sorted(result, key=lambda d: (-d['score'], d['title']))


class Fetcher:
    def __init__(self, directory, config, offline=False, refresh=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config, self.offline, self.refresh = config, offline, refresh
        proxy = config.get('proxy')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
        self.last_arxiv = 0.0
        self.last_hosts = {}

    def get(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        meta_path, body_path = self.directory / (key + '.json'), self.directory / (key + '.body')
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        age = time.time() - meta.get('time', 0)
        good = meta.get('ok') and body_path.exists()
        if good and (self.offline or (not self.refresh and age < self.config.get('cache_hours', 24) * 3600)):
            return body_path.read_bytes(), {'cached': True, 'fetched_at': meta['at']}
        if self.offline:
            raise RuntimeError('离线缓存缺失；最近状态：' + str(meta.get('error', '未获取')))
        host = urllib.parse.urlsplit(url).hostname
        host_path = self.directory / ('host-' + hashlib.sha256(host.encode()).hexdigest() + '.json')
        host_state = json.loads(host_path.read_text()) if host_path.exists() else {}
        if time.time() < host_state.get('retry_at', 0):
            raise RuntimeError('来源域名冷却中：' + host_state.get('error', '请求失败'))
        if meta and not meta.get('ok') and age < meta.get('cooldown_seconds', 3600):
            raise RuntimeError('来源冷却中：' + meta.get('error', '请求失败'))
        if 'arxiv.org' in urllib.parse.urlsplit(url).netloc:
            time.sleep(max(0, 3.1 - (time.monotonic() - self.last_arxiv)))
            self.last_arxiv = time.monotonic()
        interval = self.config.get('host_intervals_seconds', {}).get(host, 0)
        if interval:
            time.sleep(max(0, interval - (time.monotonic() - self.last_hosts.get(host, 0))))
            self.last_hosts[host] = time.monotonic()
        try:
            timeout = self.config.get('timeout_seconds', 18)
            # A socket timeout resets on each received chunk; also bound the whole
            # request so a slowly streaming source cannot consume the morning.
            alarm = threading.current_thread() is threading.main_thread() and not signal.getitimer(signal.ITIMER_REAL)[0]
            previous_handler = signal.getsignal(signal.SIGALRM) if alarm else None
            def expired(*_):
                raise TimeoutError('HTTP request exceeded total time budget')
            if alarm:
                signal.signal(signal.SIGALRM, expired)
                signal.setitimer(signal.ITIMER_REAL, timeout)
            try:
                with self.opener.open(urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': '*/*'}), timeout=timeout) as response:
                    cap = min(32_000_000, self.config.get('max_response_bytes', 12_000_000))
                    body = response.read(cap + 1)
                    if len(body) > cap:
                        raise ValueError(f'响应超过 {cap} 字节上限')
            finally:
                if alarm:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, previous_handler)
            body_path.write_bytes(body)
            stamp = now()
            dump(meta_path, {'ok': True, 'url': url, 'time': time.time(), 'at': stamp, 'sha256': hashlib.sha256(body).hexdigest()})
            return body, {'cached': False, 'fetched_at': stamp}
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            error = f'{type(exc).__name__}: {exc}'
            cooldown = 3600
            if isinstance(exc, urllib.error.HTTPError):
                retry = exc.headers.get('Retry-After', '')
                if retry.isdigit():
                    cooldown = max(cooldown, int(retry))
                elif retry:
                    try:
                        cooldown = max(cooldown, int(email.utils.parsedate_to_datetime(retry).timestamp() - time.time()))
                    except (TypeError, ValueError, OverflowError):
                        pass
                if exc.code in (403, 429):
                    dump(host_path, {'retry_at': time.time() + cooldown, 'error': error})
            dump(meta_path, {'ok': False, 'url': url, 'time': time.time(), 'at': now(), 'error': error, 'cooldown_seconds': cooldown})
            raise RuntimeError(error) from exc


def arxiv_records(body):
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom', 'o': 'http://a9.com/-/spec/opensearch/1.1/'}
    root = ET.fromstring(body)
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise ValueError('Expected arXiv Atom feed; received another document')
    papers = []
    for e in root.findall('a:entry', ns):
        raw_id = e.findtext('a:id', '', ns)
        if '/api/errors' in raw_id:
            raise ValueError('arXiv API error: ' + e.findtext('a:summary', '', ns))
        ids = {'arxiv': norm_id('arxiv', raw_id)}
        doi = e.findtext('x:doi', '', ns)
        if doi:
            ids['doi'] = doi
        published = e.findtext('a:published', '', ns)
        pdf = next((l.get('href') for l in e.findall('a:link', ns) if l.get('title') == 'pdf'), '')
        papers.append({'identifiers': ids, 'title': text(e.findtext('a:title', '', ns)), 'abstract': text(e.findtext('a:summary', '', ns)), 'authors': [text(a.findtext('a:name', '', ns)) for a in e.findall('a:author', ns)], 'year': int(published[:4]) if published else None, 'published': published, 'updated': e.findtext('a:updated', '', ns), 'venue': 'arXiv 预印本', 'publication_status': 'preprint', 'url': safe_url(raw_id), 'pdf_url': safe_url(pdf), 'fulltext_status': 'link_unverified'})
    return papers, int(root.findtext('o:totalResults', str(len(papers)), ns))


def crossref_records(body):
    data = json.loads(body)['message']
    result = []
    for e in data['items']:
        parts = e.get('published', {}).get('date-parts', [[]])[0]
        result.append({'identifiers': {'doi': e['DOI']}, 'title': text(' '.join(e.get('title', []))), 'abstract': text(e.get('abstract', '')), 'authors': [text(a.get('given', '') + ' ' + a.get('family', '')) for a in e.get('author', [])], 'year': parts[0] if parts else None, 'venue': text(' / '.join(e.get('container-title', []))), 'publication_status': e.get('type', 'unknown'), 'url': safe_url(e.get('URL')), 'pdf_url': next((safe_url(x.get('URL')) for x in e.get('link', []) if x.get('content-type') == 'application/pdf'), ''), 'fulltext_status': 'link_unverified'})
    return result, data.get('total-results', len(result))


class PMLRIndex(HTMLParser):
    def __init__(self, base, year, volume):
        super().__init__()
        self.base, self.year, self.volume = base, year, volume
        self.current, self.field, self.papers = None, '', []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and attrs.get('class') == 'paper':
            self.current = {'title': '', 'authors': ''}
        if self.current is None:
            return
        if attrs.get('class') in ('title', 'authors'):
            self.field = attrs['class']
        if tag == 'a':
            url = urllib.parse.urljoin(self.base, attrs.get('href', ''))
            if url.startswith(self.base) and url.endswith('.html'):
                self.current['url'] = url
            if url.startswith('https://proceedings.mlr.press/') and url.endswith('.pdf'):
                self.current['pdf_url'] = url

    def handle_data(self, data):
        if self.current is not None and self.field:
            self.current[self.field] += data

    def handle_endtag(self, tag):
        if tag in ('p', 'span'):
            self.field = ''
        if tag == 'div' and self.current is not None:
            d, self.current = self.current, None
            if d.get('url') and d['title'].strip():
                d.update({'identifiers': {'pmlr': f'v{self.volume}/' + d['url'].rsplit('/', 1)[1].removesuffix('.html')}, 'title': text(d['title']), 'authors': [text(a) for a in d['authors'].split(',') if text(a)], 'year': self.year, 'venue': f'ICML {self.year} · PMLR {self.volume}', 'publication_status': 'proceedings', 'abstract': '', 'fulltext_status': 'link_unverified'})
                self.papers.append(d)


class ICLRIndex(HTMLParser):
    """Official proceedings, restricted to the requested year and Conference track."""
    def __init__(self, base, year):
        super().__init__()
        self.base, self.year = base, int(year)
        self.current, self.field, self.papers = None, '', []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'li':
            self.current = {'title': '', 'authors': ''} if attrs.get('data-track') == 'conference' else None
            self.field = ''
        if self.current is None:
            return
        if tag == 'a' and attrs.get('title') == 'paper title':
            url = urllib.parse.urljoin(self.base, attrs.get('href', ''))
            parsed = urllib.parse.urlsplit(url)
            match = re.fullmatch(rf'/paper_files/paper/{self.year}/hash/([a-f0-9]{{32}})-Abstract-Conference\.html', parsed.path)
            if parsed.scheme == 'https' and parsed.netloc == 'proceedings.iclr.cc' and not parsed.query and not parsed.fragment and match:
                self.current.update(url=url, identifiers={'iclr': f'{self.year}/{match[1]}'})
                self.field = 'title'
        if tag == 'span' and 'paper-authors' in attrs.get('class', '').split():
            self.field = 'authors'

    def handle_data(self, data):
        if self.current is not None and self.field:
            self.current[self.field] += data

    def handle_endtag(self, tag):
        if tag in ('a', 'span'):
            self.field = ''
        if tag == 'li' and self.current is not None:
            d, self.current = self.current, None
            if d.get('identifiers') and d['title'].strip():
                d.update(title=text(d['title']), authors=[text(a) for a in d['authors'].split(',') if text(a)],
                         year=self.year, venue=f'ICLR {self.year} · 官方论文集', publication_status='proceedings',
                         abstract='', pdf_url='', fulltext_status='not_fetched')
                self.papers.append(d)


def source_request(source):
    kind = source['kind']
    if kind == 'arxiv':
        return 'https://export.arxiv.org/api/query?' + urllib.parse.urlencode({'search_query': source['query'], 'max_results': source['limit'], 'sortBy': 'relevance', 'start': 0})
    if kind == 'crossref':
        return f"https://api.crossref.org/journals/{source['issn']}/works?" + urllib.parse.urlencode({'query': source['query'], 'rows': source['limit'], 'select': 'DOI,title,author,published,URL,abstract,type,link,container-title'})
    if kind == 'pmlr':
        return f"https://proceedings.mlr.press/v{source['volume']}/"
    if kind == 'openreview':
        return 'https://api2.openreview.net/notes?' + urllib.parse.urlencode({'content.venueid': source['venue'], 'limit': source['limit']})
    if kind == 'iclr_proceedings':
        return f"https://proceedings.iclr.cc/paper_files/paper/{int(source['year'])}"
    raise ValueError('unknown source kind: ' + kind)


def parse_source(source, url, body):
    if source['kind'] == 'iclr_proceedings':
        p = ICLRIndex(url, source['year'])
        p.feed(body.decode('utf-8'))
        if not p.papers:
            raise ValueError('ICLR 官方论文集未解析到 Conference 论文；页面结构可能变化')
        return p.papers, len(p.papers)
    if source['kind'] == 'arxiv':
        return arxiv_records(body)
    if source['kind'] == 'crossref':
        return crossref_records(body)
    if source['kind'] == 'pmlr':
        p = PMLRIndex(url, source['year'], source['volume'])
        p.feed(body.decode('utf-8'))
        if not p.papers:
            raise ValueError('PMLR 未解析到论文；页面结构可能变化')
        return p.papers, len(p.papers)
    data = json.loads(body)
    papers = []
    for e in data['notes']:
        c = {k: v.get('value') if isinstance(v, dict) else v for k, v in e['content'].items()}
        if c.get('venueid') != source['venue']:
            continue
        papers.append({'identifiers': {'openreview': e['id']}, 'title': text(c.get('title')), 'abstract': text(c.get('abstract')), 'authors': c.get('authors', []), 'year': source['year'], 'venue': c.get('venue', source['venue']), 'publication_status': 'accepted', 'url': 'https://openreview.net/forum?id=' + e['id'], 'pdf_url': 'https://openreview.net/pdf?id=' + e['id'] if c.get('pdf') else '', 'fulltext_status': 'link_unverified'})
    return papers, data.get('count', len(papers))


def import_archive(store, root):
    for path in sorted((Path(root) / 'papers').glob('*/metadata.json')):
        d = json.loads(path.read_text())
        store.upsert({'identifiers': d['identifiers'], 'title': d['title'], 'title_zh': d.get('title_zh', ''), 'authors': [a['name'] if isinstance(a, dict) else a for a in d.get('authors', [])], 'year': d.get('year'), 'archived': True, 'archive_status': d.get('status'), 'archive_id': d['id'], 'venue': '已有精读', 'fulltext_status': 'archived'}, 'local-archive', str(path))


def collect(store, fetcher, config):
    reports = []
    for source in config['sources']:
        url = source_request(source)
        report = {'id': source['id'], 'label': source['label'], 'url': url, 'checked_at': now()}
        try:
            try:
                body, cache = fetcher.get(url)
                records, total = parse_source(source, url, body)
            except (RuntimeError, ValueError, KeyError, ET.ParseError) as primary_error:
                fallback = source.get('fallback')
                if not fallback:
                    raise
                report.update(primary_url=url, primary_error=str(primary_error)[:400], fallback_attempted=True)
                url = source_request(fallback)
                report.update(fallback_url=url)
                body, cache = fetcher.get(url)
                records, total = parse_source(fallback, url, body)
                report.update(url=url, fallback_used=True, data_source_label=fallback['label'])
            kept = new = conflicts = retained = 0
            for paper in records:
                matched = bool(signals(paper)[1])
                if not matched and not config.get('retain_unmatched_metadata', False):
                    continue
                pid, inserted = store.upsert(paper, source['id'], paper.get('url') or url)
                kept += bool(pid is not None and matched)
                retained += pid is not None
                new += inserted
                conflicts += pid is None
            report.update(cache, status='ok', fetched=len(records), total_available=total, matched=kept, retained=retained, new=new, conflicts=conflicts, coverage='bounded_sample' if total > len(records) else 'complete_response')
        except (RuntimeError, ValueError, KeyError, ET.ParseError) as exc:
            report.update(status='error', error=str(exc)[:400])
        with store.db:
            store.db.execute('INSERT INTO runs(at,source_id,data) VALUES(?,?,?)', (now(), source['id'], json.dumps(report, ensure_ascii=False)))
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return reports


def snapshot(store, reports):
    rows = store.rows()
    conflicts = [json.loads(r[0]) for r in store.db.execute('SELECT data FROM conflicts')]
    return {'generated_at': now(), 'prototype': True, 'selection_mode': 'explicit_topic_rules_with_optional_local_semantics', 'semantic_search': 'local_service_or_cli', 'sources': reports, 'topics': [{'id': x[0], 'label': x[1]} for x in TOPICS], 'papers': rows, 'conflicts': conflicts,
            'coverage': {'with_abstract': sum(bool(p.get('abstract')) for p in rows), 'title_only': sum(not p.get('abstract') for p in rows), 'possible_duplicate_records': sum(bool(p.get('possible_duplicate')) for p in rows)},
            'counts': {'candidates': sum(not x.get('archived') for x in rows), 'archived': sum(bool(x.get('archived')) for x in rows)}}


def build_page(data, out):
    view = dict(data)
    if 'papers' in data:
        archived = [p for p in data['papers'] if p.get('archived')]
        view.update(papers=archived + [p for p in data['papers'] if not p.get('archived')][:max(0, 3000-len(archived))], browse_total=len(data['papers']))
    payload = json.dumps(view, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    page = (HERE / 'template.html').read_text().replace('__PAPER_DAILY_DATA__', payload)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['collect', 'build'])
    parser.add_argument('--data-dir', type=Path, default=HERE / 'data')
    parser.add_argument('--config', type=Path, default=HERE / 'sources.json')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    store = Store(args.data_dir / 'candidates.sqlite')
    try:
        import_archive(store, LIBRARY)
        if args.command == 'collect':
            reports = collect(store, Fetcher(args.data_dir / 'cache', config, args.offline, args.refresh), config)
            dump(args.data_dir / 'sources_status.json', reports)
        else:
            p = args.data_dir / 'sources_status.json'
            reports = json.loads(p.read_text()) if p.exists() else []
        data = snapshot(store, reports)
        dump(HERE / 'candidates.json', data)
        build_page(data, HERE / 'index.html')
        print(json.dumps({'page': str(HERE / 'index.html'), **data['counts'], 'source_errors': sum(x['status'] == 'error' for x in reports)}, ensure_ascii=False))
        return 0 if any(x['status'] == 'ok' for x in reports) else 2
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
