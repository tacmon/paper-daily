import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import radar
from enrich import AbstractParser, enrich


DIGEST = '04427622d11962fd7897949df48abe75'
URL = f'https://proceedings.iclr.cc/paper_files/paper/2025/hash/{DIGEST}-Abstract-Conference.html'


def entry(url=URL, track='conference'):
    return f'<li data-track="{track}"><a title="paper title" href="{url}">Curriculum <em>Learning</em></a><span class="paper-authors">A, B</span></li>'


class ICLRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = radar.Store(Path(self.tmp.name) / 'c.sqlite')
        self.source = {'id': 'iclr-2025', 'label': 'ICLR', 'kind': 'openreview', 'venue': 'ICLR.cc/2025/Conference', 'year': 2025, 'limit': 500,
                       'fallback': {'kind': 'iclr_proceedings', 'year': 2025, 'label': 'ICLR 官方论文集'}}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_official_requested_year_and_main_track(self):
        body = (entry() + entry(track='workshop') + entry(URL.replace('/2025/', '/2024/'))
                + entry(URL.replace('proceedings.iclr.cc', 'other.example'))).encode()
        records, total = radar.parse_source(self.source['fallback'], radar.source_request(self.source['fallback']), body)
        self.assertEqual(total, 1)
        self.assertEqual(records[0]['identifiers'], {'iclr': f'2025/{DIGEST}'})
        self.assertEqual(records[0]['title'], 'Curriculum Learning')
        self.assertEqual(records[0]['authors'], ['A', 'B'])
        self.assertEqual(records[0]['pdf_url'], '')

    def test_fallback_retains_primary_failure_and_is_idempotent(self):
        class Fetch:
            def get(self, url):
                if 'openreview.net' in url:
                    raise RuntimeError('域名冷却中：HTTP Error 403')
                return entry().encode(), {'cached': True, 'fetched_at': '2026-11-18'}
        with patch('builtins.print'):
            report = radar.collect(self.store, Fetch(), {'sources': [self.source]})[0]
            repeat = radar.collect(self.store, Fetch(), {'sources': [self.source]})[0]
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(report['fallback_used'])
        self.assertIn('403', report['primary_error'])
        self.assertIn('proceedings.iclr.cc', report['url'])
        self.assertIn('openreview.net', report['primary_url'])
        self.assertEqual((report['new'], repeat['new'], len(self.store.rows())), (1, 0, 1))

    def test_failed_fallback_stays_failed(self):
        class Fetch:
            def get(self, url):
                raise RuntimeError('unavailable: ' + url)
        with patch('builtins.print'):
            report = radar.collect(self.store, Fetch(), {'sources': [self.source]})[0]
        self.assertEqual(report['status'], 'error')
        self.assertIn('primary_error', report)
        self.assertFalse(report.get('fallback_used'))
        self.assertEqual(self.store.rows(), [])

    def test_successful_primary_never_calls_fallback(self):
        seen = []
        class Fetch:
            def get(self, url):
                seen.append(url)
                return json.dumps({'notes': [], 'count': 0}).encode(), {'cached': False}
        with patch('builtins.print'):
            report = radar.collect(self.store, Fetch(), {'sources': [self.source]})[0]
        self.assertEqual(len(seen), 1)
        self.assertFalse(report.get('fallback_used'))

    def test_empty_index_fails_instead_of_false_success(self):
        with self.assertRaisesRegex(ValueError, '未解析到'):
            radar.parse_source(self.source['fallback'], URL, b'<html>Maintenance</html>')

    def test_abstract_and_pdf_enrichment_survive_recollection(self):
        records, _ = radar.parse_source(self.source['fallback'], URL, entry().encode())
        self.store.upsert(records[0], 'iclr-2025', URL)
        abstract = 'We study curriculum learning and goal-conditioned reinforcement learning under changing environments. '
        pdf = URL.replace('/hash/', '/file/').replace('-Abstract-', '-Paper-').replace('.html', '.pdf')
        class Fetch:
            def get(self, url):
                return (f'<meta name="citation_pdf_url" content="{pdf}"><p class="paper-abstract"><p>{abstract}</p></p><footer>Not abstract</footer>').encode(), {'cached': True}
        report = enrich(self.store, Fetch(), limit=1)
        self.assertEqual(report[0]['status'], 'ok')
        self.store.upsert(records[0], 'iclr-2025', URL)
        p = self.store.rows()[0]
        self.assertEqual(p['abstract'], abstract.strip())
        self.assertEqual(p['pdf_url'], pdf)
        self.assertEqual(p['fulltext_status'], 'link_unverified')
        self.store.upsert({'title': p['title'], 'identifiers': p['identifiers'], 'archived': True,
                           'fulltext_status': 'archived'}, 'local-archive', '/local/metadata.json')
        self.assertEqual(self.store.rows()[0]['fulltext_status'], 'archived')
        parser = AbstractParser()
        parser.feed('<div id="abstract">Original PMLR <em>abstract</em>.</div><p>Outside</p>')
        self.assertEqual(parser.abstract(), 'Original PMLR abstract .')


if __name__ == '__main__':
    unittest.main()
