import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse as U
import time
import harvest
import radar


def atom(ids, total):
    entries=''.join(f'<entry><id>http://arxiv.org/abs/{i}v1</id><title>Curriculum learning {i}</title><summary>Abstract for {i}</summary><published>2026-11-17T00:00:00Z</published></entry>' for i in ids)
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>{total}</o:totalResults>{entries}</feed>').encode()


class Fetch:
    def __init__(self, body=None, fail=False):self.body,self.fail,self.urls=body,fail,[]
    def get(self,url):
        self.urls.append(url)
        if self.fail:raise RuntimeError('HTTP 429 cooldown')
        return self.body,{'cached':False,'fetched_at':radar.now()}


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=radar.Store(Path(self.tmp.name)/'db.sqlite')
        self.source={'id':'new','kind':'arxiv_recent','label':'new','query':'all:curriculum','page_size':1}
        self.today=dt.date(2026,11,18)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_resume_failure_and_complete(self):
        first,state=harvest.ingest(self.source,{},self.store,Fetch(atom(['2611.00001'],2)),self.today)
        self.assertEqual(first['coverage'],'partial');self.assertNotIn('covered_through',state)
        failed,state2=harvest.ingest(self.source,state,self.store,Fetch(fail=True),self.today)
        self.assertEqual(failed['status'],'error');self.assertEqual(state2['window'],state['window'])
        fetch=Fetch(atom(['2611.00002'],2));last,state=harvest.ingest(self.source,state2,self.store,fetch,self.today)
        self.assertEqual(U.parse_qs(U.urlsplit(fetch.urls[0]).query)['start'],['1'])
        self.assertEqual(last['coverage'],'window_complete');self.assertNotIn('window',state)
        self.assertEqual(len(self.store.rows()),2)
        repeat,state=harvest.ingest(self.source,state,self.store,Fetch(atom(['2611.00001'],1)),self.today)
        self.assertEqual(repeat['new'],0);self.assertEqual(len(self.store.rows()),2)
    def test_crossref_stall_cannot_advance_checkpoint(self):
        source={'id':'journal','kind':'crossref_recent','label':'journal','issn':'x','page_size':1}
        body=json.dumps({'message':{'total-results':2,'next-cursor':'*','items':[{'DOI':'10.1/x','title':['Paper']}]}}).encode()
        report,state=harvest.ingest(source,{},self.store,Fetch(body),self.today)
        self.assertEqual(report['status'],'error');self.assertEqual(state['window']['offset'],0)
        self.assertNotIn('covered_through',state)
    def test_empty_catalogue_is_not_success(self):
        source=dict(id='program',kind='program',label='program',year=2026,url='https://icml.cc/Downloads/2026')
        report,state=harvest.ingest(source,{},self.store,Fetch(b'<h1>Coming soon</h1>'),self.today)
        self.assertEqual(report['status'],'error');self.assertNotIn('baseline_finished',state)
    def test_program_filters_nonpapers_and_wrong_hosts(self):
        source=dict(kind='program',label='program',year=2026,publication_status='public_program')
        body=b'<a class="Poster" href="/virtual/2026/poster/1">A paper</a><a class="Workshop" href="/virtual/2026/poster/2">Workshop</a><a class="Poster" href="https://bad.example/virtual/2026/poster/3">Wrong host</a>'
        records,total=harvest.parse(source,'https://icml.cc/Downloads/2026',body)
        self.assertEqual(total,1);self.assertEqual(records[0]['publication_status'],'public_program');self.assertFalse(records[0]['pdf_url'])
    def test_snapshot_diff_not_publication_date(self):
        source=dict(id='program',kind='program',label='program',year=2026,url='https://icml.cc/Downloads/2026')
        body=b'<a class="Poster" href="/virtual/2026/poster/1">Old paper</a>'
        first,state=harvest.ingest(source,{},self.store,Fetch(body),self.today)
        self.assertTrue(first['baseline']);stamp=self.store.rows()[0]['first_seen_at']
        repeat,state=harvest.ingest(source,state,self.store,Fetch(body),self.today)
        self.assertTrue(repeat['unchanged']);self.assertEqual(repeat['new'],0)
        changed,state=harvest.ingest(source,state,self.store,Fetch(body+b'<a class="Poster" href="/virtual/2026/poster/2">Another paper</a>'),self.today)
        self.assertFalse(changed['baseline']);self.assertEqual(changed['new'],1)
        old=next(p for p in self.store.rows() if p['title']=='Old paper')
        self.assertEqual(old['first_seen_at'],stamp);self.assertEqual(old['discovery_batch'],'baseline')
        self.assertNotIn('published',old)
        self.assertEqual(old['authors'],[])
    def test_acl_inline_title_doi_and_abstract(self):
        body=b'<collection id="2026.acl"><volume id="long" ingest-date="2026-06-01"><paper id="1"><title><fixed-case>R</fixed-case>L reasoning</title><author><first>A</first><last>B</last></author><abstract>Some evidence</abstract><doi>10.1/ABC</doi></paper></volume></collection>'
        records,total=harvest.parse(dict(kind='acl_xml',label='ACL',year=2026),'https://aclanthology.org',body)
        self.assertEqual(total,1);p=records[0];self.assertEqual(p['title'],'RL reasoning');self.assertEqual(p['authors'],['A B'])
        self.store.upsert(p,'acl','https://aclanthology.org')
        self.store.upsert(dict(title=p['title'],identifiers={'doi':'10.1/abc'}),'crossref','https://api.crossref.org')
        self.assertEqual(len(self.store.rows()),1)
    def test_registry_venue_year_and_truncation(self):
        config={'recent_years':2,'pmlr_families':{'ICML':r'\bICML\b'}}
        body=b'<ul><li><a href="v1">Volume 1</a> ICML 2025</li><li><a href="v2">Volume 2</a> ICML 2026</li><li><a href="v3">Volume 3</a> ICML 2024</li><li><a href="v4">Volume 4</a> Other 2026</li></ul>'
        records=harvest.discover(body,dict(kind='pmlr_catalog',url='https://proceedings.mlr.press/'),2026,config)
        self.assertEqual([r['id'] for r in records],['pmlr-1','pmlr-2'])
        with self.assertRaises(ValueError):harvest.discover(b'{"truncated":true,"tree":[]}',dict(kind='acl_catalog'),2026,config)
    def test_pending_window_does_not_block_fresh_revision_query(self):
        source=dict(self.source,kind='arxiv_frontier');url=harvest.request(source,{'window':{'offset':1000}},self.today)
        query=U.parse_qs(U.urlsplit(url).query)
        self.assertEqual(query['sortBy'],['lastUpdatedDate']);self.assertEqual(query['start'],['0'])
    def test_cached_sample_count_is_not_api_total(self):
        source=dict(self.source,kind='arxiv_frontier')
        fetch=Fetch(atom(['2611.00001'],5000))
        first,state=harvest.ingest(source,{},self.store,fetch,self.today)
        repeat,_=harvest.ingest(source,state,self.store,fetch,self.today)
        self.assertEqual(repeat['fetched'],1);self.assertEqual(repeat['total_results'],5000)
    def test_whole_request_timeout(self):
        fetch=radar.Fetcher(Path(self.tmp.name)/'cache',{'timeout_seconds':0.02})
        class Slow:
            def open(self,*a,**k):time.sleep(0.2);raise AssertionError('deadline did not interrupt')
        fetch.opener=Slow()
        with self.assertRaisesRegex(RuntimeError,'total time budget'):fetch.get('https://publisher.example/papers')
    def test_publisher_abstract_heading(self):
        from enrich import AbstractParser
        p=AbstractParser();p.feed('<h4>Abstract</h4><p>Meaningful <em>paper</em> abstract.</p><h4>References</h4><p>Not abstract</p>')
        self.assertEqual(p.abstract(),'Meaningful paper abstract.')
    def test_multiple_pages_complete_within_run(self):
        class Pages:
            def get(inner,url):
                start=int(U.parse_qs(U.urlsplit(url).query)['start'][0])
                return atom([f'2611.0000{start+1}'],2),{'cached':False,'fetched_at':radar.now()}
        config={'feeds':[self.source],'catalogues':[],'max_pages_per_source':3}
        with patch.object(radar,'HERE',Path(self.tmp.name)):
            reports=harvest.collect(self.store,Pages(),config,seconds=5)
        self.assertEqual(reports[0]['coverage'],'window_complete')
        self.assertEqual((reports[0]['pages_this_run'],reports[0]['fetched'],reports[0]['new']),(2,2,2))
    def test_html_error_is_not_empty_arxiv_result(self):
        with self.assertRaisesRegex(ValueError,'Atom feed'):radar.arxiv_records(b'<html><body>Unavailable</body></html>')
    def test_slow_catalogues_leave_time_for_new_papers(self):
        clock=[0.0]
        class SlowCatalog:
            def get(inner,url):
                if 'catalog' in url:
                    clock[0]+=6
                    raise RuntimeError('catalogue stalled')
                return atom(['2611.00001'],1),{'cached':False,'fetched_at':radar.now()}
        config={'feeds':[dict(self.source,kind='arxiv_frontier')], 'catalogues':[
            dict(id=f'catalog-{i}',kind='pmlr_catalog',label='catalog',url=f'https://catalog.example/{i}') for i in range(2)]}
        with patch.object(radar,'HERE',Path(self.tmp.name)), patch('harvest.time.monotonic',side_effect=lambda:clock[0]):
            reports=harvest.collect(self.store,SlowCatalog(),config,seconds=20)
        self.assertEqual([r['status'] for r in reports],['error','pending','ok'])
        self.assertEqual(len(self.store.rows()),1)


if __name__=='__main__':unittest.main()
