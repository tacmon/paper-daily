import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import radar


def paper(ids, title='Goal-conditioned reinforcement learning'):
    return {'identifiers': ids, 'title': title, 'authors': [], 'abstract': ''}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = radar.Store(Path(self.tmp.name) / 'c.sqlite')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_versions_share_record(self):
        first, _ = self.store.upsert(paper({'arxiv': '1705.06366v1'}), 'arxiv', 'https://arxiv.org/abs/1705.06366v1')
        second, new = self.store.upsert(paper({'arxiv': 'https://arxiv.org/abs/1705.06366v3', 'doi': 'https://doi.org/10.1/ABC'}), 'arxiv', 'https://arxiv.org/abs/1705.06366v3')
        self.assertEqual(first, second)
        self.assertFalse(new)
        self.assertEqual(len(self.store.rows()), 1)
        self.assertEqual(self.store.rows()[0]['identifiers']['doi'], '10.1/abc')

    def test_repeat_import_is_idempotent(self):
        p = paper({'doi': '10.1/a'})
        self.store.upsert(p, 'journal', 'https://doi.org/10.1/a')
        self.store.upsert(p, 'journal', 'https://doi.org/10.1/a')
        rows = self.store.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['sources']), 1)

    def test_conflicting_bridge_is_not_merged(self):
        self.store.upsert(paper({'arxiv': '1234.56789'}), 'a', 'a')
        self.store.upsert(paper({'doi': '10.1/a'}), 'b', 'b')
        pid, new = self.store.upsert(paper({'arxiv': '1234.56789', 'doi': '10.1/a'}), 'c', 'c')
        self.assertIsNone(pid)
        self.assertFalse(new)
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM conflicts').fetchone()[0], 1)

    def test_same_title_is_warning_not_merge(self):
        self.store.upsert(paper({'arxiv': '1234.56789'}), 'a', 'a')
        self.store.upsert(paper({'doi': '10.1/a'}), 'b', 'b')
        self.assertTrue(all(x['possible_duplicate'] for x in self.store.rows()))

    def test_archived_paper_excluded_from_new_candidates(self):
        p = paper({'arxiv': '1705.06366'})
        p.update(archived=True, title_zh='已有精读', year=2018)
        self.store.upsert(p, 'local', 'local')
        q = paper({'arxiv': '1705.06366v2'}, title='Source title')
        q['year'] = 2017
        self.store.upsert(q, 'remote', 'remote')
        data = radar.snapshot(self.store, [])
        self.assertEqual(data['counts'], {'archived': 1, 'candidates': 0})
        self.assertEqual(data['papers'][0]['year'], 2018)

    def test_pmlr_uses_official_landing_page(self):
        body='<div class="paper"><p class="title">Non-stationary Reinforcement Learning</p><p><span class="authors">A,&nbsp;B</span></p><p><a href="smith23a.html">abs</a><a href="https://proceedings.mlr.press/v202/smith23a/smith23a.pdf">PDF</a></p></div>'
        parser = radar.PMLRIndex('https://proceedings.mlr.press/v202/', 2023, '202')
        parser.feed(body)
        self.assertEqual(parser.papers[0]['identifiers'], {'pmlr': 'v202/smith23a'})
        self.assertEqual(parser.papers[0]['authors'], ['A', 'B'])

    def test_arxiv_does_not_mark_pdf_verified(self):
        body=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1234.56789v2</id><title>Goal generation</title><published>2020-01-01</published><link title="pdf" href="http://arxiv.org/pdf/1234.56789v2"/></entry></feed>'
        records, _ = radar.arxiv_records(body)
        self.assertEqual(records[0]['identifiers']['arxiv'], '1234.56789')
        self.assertEqual(records[0]['fulltext_status'], 'link_unverified')

    def test_openreview_only_accepted_venue(self):
        source={'kind':'openreview','venue':'ICLR.cc/2025/Conference','year':2025}
        def note(i,venue):
            return {'id':i,'content':{'venueid':{'value':venue},'title':{'value':'Curriculum learning'}}}
        records, _=radar.parse_source(source,'',json.dumps({'notes':[note('a',source['venue']),note('b','rejected')],'count':2}).encode())
        self.assertEqual([x['identifiers']['openreview'] for x in records], ['a'])

    def test_error_does_not_block_next_source(self):
        class Fetch:
            def get(self,url):
                if 'arxiv' in url:raise RuntimeError('HTTP 429')
                return json.dumps({'message':{'items':[],'total-results':0}}).encode(), {'cached':False}
        sources=[{'id':'a','kind':'arxiv','label':'a','query':'q','limit':1},{'id':'b','kind':'crossref','label':'b','issn':'1234','query':'q','limit':1}]
        with patch('builtins.print'):
            reports=radar.collect(self.store,Fetch(),{'sources':sources})
        self.assertEqual([r['status'] for r in reports],['error','ok'])
        self.assertEqual(reports[1]['fetched'],0)

    def test_broad_metadata_retention_keeps_unmatched_titles(self):
        source={'id':'b','kind':'crossref','label':'b','issn':'1234','query':'q','limit':1}
        class Fetch:
            def get(self,url):return b'{}',{'cached':True}
        item=paper({'doi':'10.1/unmatched'},title='An unrelated method with alternate terminology')
        with patch.object(radar,'parse_source',return_value=([item],1)),patch('builtins.print'):
            first=radar.collect(self.store,Fetch(),{'sources':[source]})
            second=radar.collect(self.store,Fetch(),{'sources':[source],'retain_unmatched_metadata':True})
        self.assertEqual(first[0]['retained'],0)
        self.assertEqual(second[0]['retained'],1)
        self.assertEqual(second[0]['matched'],0)

    def test_offline_never_calls_network(self):
        f=radar.Fetcher(Path(self.tmp.name)/'cache',{},offline=True)
        with patch.object(f.opener,'open',side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(RuntimeError,'缓存缺失'):f.get('https://example.org')

    def test_failure_cooldown_prevents_retry_even_refresh(self):
        f=radar.Fetcher(Path(self.tmp.name)/'cache',{},refresh=True)
        import hashlib,time
        url='https://example.org'
        radar.dump(f.directory/(hashlib.sha256(url.encode()).hexdigest()+'.json'),{'ok':False,'time':time.time(),'error':'429','cooldown_seconds':3600})
        with patch.object(f.opener,'open',side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(RuntimeError,'冷却'):f.get(url)

    def test_untrusted_title_cannot_close_data_script(self):
        path=Path(self.tmp.name)/'index.html'
        radar.build_page({'title':'</script><script>alert(1)</script>'},path)
        s=path.read_text()
        self.assertNotIn('</script><script>alert(1)',s)
        self.assertIn('\\u003c/script>',s)
        self.assertEqual(radar.safe_url('javascript:alert(1)'), '')

    def test_429_stops_other_queries_on_same_host(self):
        import urllib.error
        from email.message import Message
        headers=Message();headers['Retry-After']='7200'
        error=urllib.error.HTTPError('https://example.org/first',429,'rate limited',headers,None)
        f=radar.Fetcher(Path(self.tmp.name)/'cache',{})
        with patch.object(f.opener,'open',side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError,'429'):f.get('https://example.org/first')
            with self.assertRaisesRegex(RuntimeError,'域名冷却'):f.get('https://example.org/second')
            self.assertEqual(request.call_count,1)


if __name__=='__main__':
    unittest.main()
