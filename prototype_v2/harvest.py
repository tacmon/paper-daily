#!/usr/bin/env python3
"""Daily metadata discovery: dated queries, official catalogues and resumable pages.

No model calls, paper selection, PDF downloads or publication. All dates of discovery
are local observations, never asserted to be the publisher's announcement date.
"""
from __future__ import annotations
import argparse
import copy
import fcntl
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import re
import time
import urllib.parse as U
import xml.etree.ElementTree as ET
import radar


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.walk(tag)

    def text(self):
        return ' '.join(' '.join(c.text() if isinstance(c, Node) else c for c in self.children).split())


class Tree(HTMLParser):
    void = {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr', 'source', 'area', 'base', 'embed'}

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(body.decode('utf-8'))

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].children.append(n)
        if tag not in self.void:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in ('script', 'style'):
            self.stack[-1].children.append(data)


def joined(node, tag):
    return ' '.join(n.text() for n in node.walk(tag))


def base_paper(source, identifiers, title, url, authors=(), abstract='', pdf='', **extra):
    return dict(identifiers=identifiers, title=radar.text(title), url=radar.safe_url(url),
                authors=list(authors), abstract=radar.text(abstract), pdf_url=radar.safe_url(pdf),
                year=source.get('year'), venue=source['label'],
                publication_status=source.get('publication_status', 'proceedings'),
                fulltext_status='link_unverified' if pdf else 'not_fetched', **extra)


def parse(source, url, body):
    kind = source['kind']
    if kind in ('arxiv', 'arxiv_recent', 'arxiv_frontier', 'arxiv_seeds'):
        return radar.arxiv_records(body)
    if kind in ('crossref', 'crossref_recent', 'crossref_frontier'):
        return radar.crossref_records(body)
    if kind in ('pmlr', 'iclr_proceedings', 'openreview'):
        papers, total = radar.parse_source(source, url, body)
        for p in papers:
            p['venue'] = source['label']
        return papers, total
    if kind == 'acl_xml':
        root = ET.fromstring(body)
        result = []
        def txt(e, name):
            n = e.find(name)
            return ''.join(n.itertext()) if n is not None else ''
        for volume in root.findall('volume'):
            for e in volume.findall('paper'):
                pid = f"{root.attrib['id']}-{volume.attrib['id']}.{e.attrib['id']}"
                ids = {'acl': pid}
                if txt(e, 'doi'):
                    ids['doi'] = txt(e, 'doi')
                result.append(base_paper(source, ids, txt(e, 'title'), f'https://aclanthology.org/{pid}/',
                    [f"{txt(a, 'first')} {txt(a, 'last')}".strip() for a in e.findall('author')],
                    txt(e, 'abstract'), f'https://aclanthology.org/{pid}.pdf',
                    publisher_ingest_date=volume.get('ingest-date', '')))
        return result, len(result)
    if kind == 'jmlr_rss':
        result = []
        for e in ET.fromstring(body).findall('./channel/item'):
            link = e.findtext('link', '').strip().replace('http:', 'https:')
            m = re.search(r'/papers/(v\d+/[^/]+)\.html', link)
            if m:
                p = base_paper(source, {'jmlr': m[1]}, e.findtext('title'), link,
                    [e.findtext('author', '')], e.findtext('description', ''),
                    e.findtext('pdf', '').strip().replace('http:', 'https:'))
                year = re.search(r'20\d{2}', e.findtext('pubDate', ''))
                p['year'] = int(year[0]) if year else None
                result.append(p)
        return result, len(result)
    if kind == 'publisher_paper':
        from enrich import AbstractParser
        tree = Tree(body).root
        metas = list(tree.walk('meta'))
        def meta(name):
            return [m.attrs.get('content','') for m in metas if m.attrs.get('name')==name]
        title = meta('citation_title')
        if not title:
            raise ValueError('Official paper citation metadata missing')
        abstract = AbstractParser(); abstract.feed(body.decode('utf-8'))
        return [base_paper(source, source['identifiers'], title[0], url, meta('citation_author'), abstract.abstract(), (meta('citation_pdf_url') or [''])[0], classic_seed=True)], 1
    tree, result = Tree(body).root, []
    if kind == 'program':
        # Official public programme only: exclude talks, tutorials and workshops.
        host = U.urlsplit(url).netloc
        for a in tree.walk('a'):
            link = U.urljoin(url, a.attrs.get('href', ''))
            m = re.fullmatch(rf'/virtual/{source["year"]}/poster/(\d+)', U.urlsplit(link).path)
            if m and U.urlsplit(link).netloc == host and 'Poster' in a.attrs.get('class', '').split():
                result.append(base_paper(source, {'conference_event': f'{host}/{source["year"]}/{m[1]}'}, a.text(), link))
    elif kind == 'neurips':
        for li in tree.walk('li'):
            for a in li.walk('a'):
                link = U.urljoin(url, a.attrs.get('href', ''))
                m = re.fullmatch(rf'/paper_files/paper/{source["year"]}/hash/([a-f0-9]{{32}})-Abstract(?:-[\w]+)?\.html', U.urlsplit(link).path)
                if m and U.urlsplit(link).netloc == 'proceedings.neurips.cc':
                    authors = joined(li, 'i') or ' '.join(n.text() for n in li.walk('span') if 'author' in n.attrs.get('class', ''))
                    result.append(base_paper(source, {'neurips': f'{source["year"]}/{m[1]}'}, a.text(), link, authors.split(',')))
    elif kind == 'tmlr':
        for li in tree.walk('li'):
            title = joined(li, 'h4')
            if not title:
                continue
            links = [a.attrs.get('href', '') for a in li.walk('a')]
            link = next((x for x in links if x.startswith('https://openreview.net/forum?id=')), '')
            pid = U.parse_qs(U.urlsplit(link).query).get('id', [''])[0]
            if pid:
                year = re.search(r'20\d{2}', li.text())
                p = base_paper(source, {'openreview': pid}, title, link, joined(li, 'i').split(','),
                    pdf=next((x for x in links if x.startswith('https://openreview.net/pdf?id=')), ''))
                p['year'] = int(year[0]) if year else None
                result.append(p)
    elif kind in ('jmlr_volume', 'rlj'):
        for dl in tree.walk('dl'):
            # RLJ has one dl per paper; JMLR may have several dt/dd pairs.
            title = ''
            for child in dl.children:
                if not isinstance(child, Node):
                    continue
                if child.tag == 'dt':
                    title = child.text()
                if child.tag != 'dd' or not title:
                    continue
                links = {a.text(): U.urljoin(url, a.attrs.get('href', '')) for a in child.walk('a')}
                link = links.get('abs', '')
                if not link:
                    continue
                pid = U.urlsplit(link).path.removeprefix('/papers/').removesuffix('.html')
                authors = joined(child, 'i') or ' '.join(n.text() for n in child.walk('span') if n.attrs.get('class') == 'paper-authors')
                p = base_paper(source, {('jmlr' if kind == 'jmlr_volume' else 'rlj'): pid}, title, link, authors.split(','), pdf=links.get('pdf', ''))
                if kind == 'rlj' and 'Pre-proceedings' in body.decode('utf-8')[:18000]:
                    p['publication_status'] = 'pre_proceedings'
                result.append(p)
    else:
        raise ValueError('Unknown discovery adapter: ' + kind)
    # Mirrored menu/rendered entries must not inflate counts.
    result = list({json.dumps(p['identifiers'], sort_keys=True): p for p in result}.values())
    return result, len(result)


def discover(body, source, year, config):
    """Only follow observed official catalogue links, within declared venue/year scope."""
    minimum = year - config.get('recent_years', 2) + 1
    if source['kind'] == 'acl_catalog':
        payload = json.loads(body)
        entries = payload.get('tree', [])
        if payload.get('truncated') or not entries:
            raise ValueError('ACL directory response may be truncated; do not claim complete registry')
        result = []
        for e in entries:
            name = e.get('path', '')
            m = re.fullmatch(r'(20\d{2})\.(acl|emnlp|findings(?:-acl|-emnlp)?)\.xml', name)
            if m and minimum <= int(m[1]) <= year:
                result.append(dict(id='acl-'+name.removesuffix('.xml'), kind='acl_xml', year=int(m[1]), label=f'ACL Anthology · {name.removesuffix(".xml")}', url='https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/'+name))
        return result
    if source['kind'] == 'jmlr_catalog':
        result = {}
        for e in ET.fromstring(body).findall('./channel/item'):
            m = re.search(r'/papers/(v\d+)/', e.findtext('link', ''))
            y = re.search(r'20\d{2}', e.findtext('pubDate', ''))
            if m and y and minimum <= int(y[0]) <= year:
                result[m[1]] = dict(id='jmlr-'+m[1], kind='jmlr_volume', year=int(y[0]), label=f'JMLR {y[0]} · {m[1]}', url=f'https://jmlr.org/papers/{m[1]}/')
        return list(result.values())
    tree, found = Tree(body).root, {}
    family_patterns = config['pmlr_families']
    for container in tree.walk():
        if container.tag not in ('li', 'p'):
            continue
        label = container.text()
        years = re.findall(r'\b20\d{2}\b', label)
        for a in container.walk('a'):
            link = U.urljoin(source['url'], a.attrs.get('href', ''))
            host, path = U.urlsplit(link).netloc, U.urlsplit(link).path
            entry = None
            if source['kind'] == 'pmlr_catalog' and host == 'proceedings.mlr.press':
                m = re.fullmatch(r'/v(\d+)/?', path)
                family = next((f for f, pattern in family_patterns.items() if re.search(pattern, label, re.I)), None)
                if m and years and family:
                    y = int(years[-1]); entry = dict(id=f'pmlr-{m[1]}', kind='pmlr', volume=m[1], year=y, label=f'{family} {y} · PMLR', url=link.rstrip('/')+'/')
            elif source['kind'] in ('neurips_catalog', 'iclr_catalog'):
                m = re.fullmatch(r'/paper_files/paper/(20\d{2})(?:/[^/]+)?/?', path)
                expected = 'proceedings.neurips.cc' if source['kind'] == 'neurips_catalog' else 'proceedings.iclr.cc'
                if m and host == expected and 'Creative AI' not in a.text():
                    family = 'NeurIPS' if source['kind'] == 'neurips_catalog' else 'ICLR'
                    y = int(m[1]); entry = dict(id=f'{family.lower()}-{y}-proceedings', kind='neurips' if family == 'NeurIPS' else 'iclr_proceedings', year=y, label=f'{family} {y} · 正式论文集', url=link)
            elif source['kind'] == 'rlj_catalog' and host == 'rlj.org':
                m = re.fullmatch(r'/volumes/(\d+)', path)
                if m and years:
                    y = int(years[-1]); entry = dict(id=f'rlj-{m[1]}', kind='rlj', year=y, label=f'RLJ/RLC {y} · 卷 {m[1]}', url=link)
            if entry and minimum <= entry['year'] <= year:
                found[entry['id']] = entry
    return list(found.values())


class State:
    def __init__(self, store):
        self.db = store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS harvest_state(id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def get(self, key):
        row = self.db.execute('SELECT data FROM harvest_state WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(self, key, data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO harvest_state VALUES(?,?)', (key, json.dumps(data, ensure_ascii=False)))


def request(source, state, today):
    kind = source['kind']
    if kind in ('arxiv_recent', 'crossref_recent'):
        if not state.get('window'):
            watermark = state.get('covered_through')
            since = dt.date.fromisoformat(watermark[:10]) - dt.timedelta(days=source.get('overlap_days', 7)) if watermark else today-dt.timedelta(days=source.get('initial_days', 30))
            until = dt.datetime.now(dt.timezone.utc).isoformat(timespec='minutes')
            state['window'] = {'since': since.isoformat(), 'until': until, 'offset': 0, 'cursor': '*', 'count': 0, 'new': 0}
        w = state['window']; size = source.get('page_size', 100)
        if kind == 'arxiv_recent':
            end = dt.datetime.fromisoformat(w['until']).strftime('%Y%m%d%H%M')
            query = f'({source["query"]}) AND submittedDate:[{w["since"].replace("-", "")}0000 TO {end}]'
            return 'https://export.arxiv.org/api/query?' + U.urlencode(dict(search_query=query, sortBy='submittedDate', sortOrder='descending', start=w['offset'], max_results=size))
        return f'https://api.crossref.org/journals/{source["issn"]}/works?' + U.urlencode(dict(filter=f'from-index-date:{w["since"]},until-index-date:{w["until"][:10]}', cursor=w['cursor'], rows=size))
    if kind == 'arxiv_frontier':
        return 'https://export.arxiv.org/api/query?' + U.urlencode(dict(search_query=source['query'], sortBy='lastUpdatedDate', sortOrder='descending', start=0, max_results=source.get('page_size',100)))
    if kind == 'crossref_frontier':
        return f'https://api.crossref.org/journals/{source["issn"]}/works?' + U.urlencode(dict(sort='indexed', order='desc', rows=source.get('page_size',100)))
    if kind == 'arxiv_seeds':
        return 'https://export.arxiv.org/api/query?' + U.urlencode(dict(id_list=','.join(source['ids']), max_results=len(source['ids'])))
    if kind == 'openreview':
        return radar.source_request(source) + '&offset=' + str(state.get('offset', 0))
    return source.get('url') or radar.source_request(source)


def ingest(source, state, store, fetcher, today):
    """One transactional checkpoint per successful page; retries are idempotent."""
    report = {k: source[k] for k in ('id', 'kind', 'label')}
    report.update(at=radar.now(), new=0, status='error', coverage='unknown')
    fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
    if state.get('definition') != fingerprint:
        state = {'definition': fingerprint}
    checkpoint = copy.deepcopy(state)
    try:
        url = request(source, state, today)
        checkpoint = copy.deepcopy(state)
        report['url'] = url
        body, cache = fetcher.get(url)
        digest = hashlib.sha256(body).hexdigest()
        incremental = source['kind'] in ('arxiv_recent', 'crossref_recent', 'openreview')
        if not incremental and state.get('body_sha256') == digest and 'fetched' in state:
            report.update(status='ok', coverage=state.get('coverage', 'complete_response'), fetched=state['fetched'], total_results=state.get('total'), unchanged=True, **cache)
        else:
            papers, total = parse(source, url, body)
            if not papers and total and total > 0:
                raise ValueError('Expected records but parser returned no papers; cursor unchanged')
            if not papers and not incremental:
                raise ValueError('No records: source not published yet or schema changed; not a successful empty check')
            baseline = not state.get('baseline_finished')
            for paper in papers:
                paper['discovery_batch'] = 'baseline' if baseline else 'incremental'
                _, fresh = store.upsert(paper, source['id'], url)
                report['new'] += int(fresh)
            complete = True
            if source['kind'] in ('arxiv_recent', 'crossref_recent'):
                w = state['window']; w['offset'] += len(papers); w['count'] += len(papers); w['new'] += report['new']
                if source['kind'] == 'crossref_recent':
                    cursor = json.loads(body)['message'].get('next-cursor')
                    complete = w['offset'] >= total or not papers
                    if not complete and (not cursor or cursor == w['cursor']):
                        raise ValueError('Crossref pagination stalled; window not marked complete')
                    w['cursor'] = cursor
                else:
                    complete = w['offset'] >= total
                report.update(window=dict(w), total_results=total, fetched=len(papers))
                if complete:
                    state['covered_through'] = w['until']; state.pop('window')
                report['coverage'] = 'window_complete' if complete else 'partial'
            elif source['kind'] == 'openreview':
                state['offset'] = state.get('offset', 0) + len(papers)
                complete = state['offset'] >= total
                report.update(fetched=len(papers), total_results=total, coverage='complete_response' if complete else 'partial')
                if complete:
                    state['offset'] = 0
            else:
                coverage = 'bounded_sample' if source['kind'] in ('arxiv_frontier','crossref_frontier') else 'feed_snapshot' if source['kind']=='jmlr_rss' else 'complete_response'
                report.update(fetched=len(papers), total_results=total, coverage=coverage)
            report['baseline'] = baseline
            if complete:
                state['baseline_finished'] = True
            state.update(body_sha256=digest, total=total, fetched=len(papers), coverage=report['coverage'])
            report.update(status='ok', **cache)
        state.update(last_success=radar.now(), last_data_at=report['fetched_at'])
    except (RuntimeError, ValueError, UnicodeError, ET.ParseError, KeyError, TypeError) as exc:
        state = checkpoint
        report.update(status='error', error=f'{type(exc).__name__}: {exc}'[:300])
    state['last_attempt'] = radar.now()
    report.update(covered_through=state.get('covered_through'), last_success=state.get('last_success'), pending_window=state.get('window'))
    state['report'] = report
    return report, state


def _collect(store, fetcher, config, seconds=150, only=None):
    start = time.monotonic(); today = dt.datetime.now(dt.timezone.utc).date(); year = today.year
    state = State(store); reports = []; sources = []
    # Public announcement/programme + fresh feeds precede historical corpus expansion.
    for s in config['feeds']:
        s = dict(s)
        if '{year}' in json.dumps(s):
            s = json.loads(json.dumps(s).replace('{year}', str(year)))
        if 'year' in s:
            s['year'] = int(s['year'])
        sources.append(s)
    # Reserve most of the run for fresh-paper feeds, even when catalogues stall.
    catalog_budget = min(30, seconds * .25)
    catalogues = sorted(config['catalogues'], key=lambda c: state.get(c['id']).get('last_attempt', ''))
    for catalog in catalogues:
        saved = state.get(catalog['id'])
        report = dict(id=catalog['id'], kind=catalog['kind'], label=catalog['label'], at=radar.now(), url=catalog['url'])
        if time.monotonic()-start >= catalog_budget or only:
            report.update(status='pending', coverage='not_attempted', error='本轮时间预算/指定来源；保留已发现目录')
        else:
            saved['last_attempt'] = radar.now()
            try:
                body, cache = fetcher.get(catalog['url'])
                if catalog['kind'] == 'acl_catalog':
                    for folder in ('data', 'xml'):
                        entry = next(e for e in json.loads(body)['tree'] if e['path'] == folder and e['type'] == 'tree')
                        link = entry['url']
                        if not re.fullmatch(r'https://api\.github\.com/repos/acl-org/acl-anthology/git/trees/[a-f0-9]{40}', link):
                            raise ValueError('Unexpected ACL tree URL')
                        body, cache = fetcher.get(link)
                found = discover(body, catalog, year, config)
                if not found:
                    raise ValueError('No in-scope volumes found; catalogue requires review')
                saved.update(sources=found, last_success=radar.now())
                report.update(status='ok', coverage='catalogue', discovered_volumes=len(found), **cache)
            except (RuntimeError, ValueError, UnicodeError, KeyError, StopIteration, ET.ParseError) as exc:
                report.update(status='error', coverage='unknown', error=str(exc)[:300])
        report['last_success'] = saved.get('last_success'); saved['report'] = report
        state.put(catalog['id'], saved); reports.append(report); sources.extend(saved.get('sources', []))
    # Source-year registry survives errors; ageing volumes stop consuming daily requests.
    sources = list({s['id']: s for s in sources}.values())
    # Fair rotation inside each priority class by the last actual attempt.
    sources.sort(key=lambda s: (s.get('priority', 2), state.get(s['id']).get('last_attempt', '')))
    for source in sources:
        saved = state.get(source['id'])
        if (only and source['id'] not in only) or time.monotonic()-start >= seconds:
            report = dict(saved.get('report', {}), id=source['id'], label=source['label'], kind=source['kind'], status='pending', coverage='not_attempted', at=radar.now(), new=0,
                          last_success=saved.get('last_success'), covered_through=saved.get('covered_through'), error='本轮时间预算/指定来源；下次续接')
        else:
            count = added = pages = 0
            for _ in range(config.get('max_pages_per_source', 3)):
                if pages and time.monotonic()-start >= seconds:
                    break
                report, saved = ingest(source, saved, store, fetcher, today)
                state.put(source['id'], saved)
                count += report.get('fetched', 0); added += report.get('new', 0); pages += 1
                if report['status'] != 'ok' or report['coverage'] != 'partial':
                    break
            report.update(fetched=count, new=added, pages_this_run=pages)
            saved['report'] = report
            state.put(source['id'], saved)
        reports.append(report)
    radar.dump(radar.HERE/'data/harvest-status.json', {'at':radar.now(), 'elapsed_seconds':round(time.monotonic()-start, 2),
        'reports':reports, 'new_records':sum(r.get('new', 0) for r in reports),
        'warning':'首次导入是历史基线；发现时间不等于发表/放榜时间。查询与公开目录有范围限制，非全领域覆盖。'})
    return reports


def collect(store, fetcher, config, seconds=150, only=None):
    # Serialise manual checks and the scheduled collector against the same DB.
    from pathlib import Path
    lock_path = Path(store.db.execute('PRAGMA database_list').fetchone()[2]).with_suffix('.harvest.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('同一论文库已有采集正在运行；本轮不并发修改分页进度')
        try:
            return _collect(store, fetcher, config, seconds, only)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--seconds', type=int, default=150)
    p.add_argument('--offline', action='store_true'); p.add_argument('--only', action='append')
    args = p.parse_args(); config=json.loads((radar.HERE/'sources.json').read_text())
    store=radar.Store(radar.HERE/'data/candidates.sqlite')
    try:
        reports=collect(store,radar.Fetcher(radar.HERE/'data/cache',config,offline=args.offline),json.loads((radar.HERE/'discovery.json').read_text()),args.seconds,args.only)
        previous=radar.HERE/'data/sources_status.json'
        old=json.loads(previous.read_text()) if previous.exists() else []
        legacy=[r for r in old if r['id'] in {s['id'] for s in config['sources']}]
        radar.dump(previous,legacy+reports)
        data=radar.snapshot(store,legacy+reports);radar.dump(radar.HERE/'candidates.json',data);radar.build_page(data,radar.HERE/'index.html')
        print(json.dumps({'counts':data['counts'],'status':{s:sum(r['status']==s for r in reports) for s in ('ok','error','pending')},'new':sum(r.get('new',0) for r in reports),'errors':[{k:r.get(k) for k in ('id','error')} for r in reports if r['status']=='error']},ensure_ascii=False))
    finally:
        store.close()


if __name__=='__main__':
    main()
