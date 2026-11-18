import array
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import radar
import semantic as s


def paper(pid,title='Curriculum learning',abstract='Learning through progressively challenging tasks.',archived=False):
    return {'id':pid,'title':title,'abstract':abstract,'identifiers':{'pmlr':'v202/'+pid},'authors':[],
            'sources':[{'source_id':'test'}],'topics':[{'id':'curriculum'}],'archived':archived}


class FakeEncoder:
    calls=[]
    def encode(self,texts,query=False):
        self.calls.append((list(texts),query))
        vector=array.array('f',[1.]+[0.]*(s.SPEC['dimension']-1)).tobytes()
        return [(vector,False) for _ in texts]


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        FakeEncoder.calls=[]
        self.idx=s.Index(self.root/'vectors.sqlite',FakeEncoder)

    def tearDown(self):
        self.idx.close();self.temp.cleanup()

    def test_incremental_reuse_and_changed_text(self):
        rows=[paper('a'),paper('b')]
        self.assertEqual(self.idx.update(rows)['encoded'],2)
        self.assertEqual(self.idx.update(rows)['encoded'],0)
        rows[0]['abstract']='New evidence changes what this document contains.'
        status=self.idx.update(rows)
        self.assertEqual((status['encoded'],status['reused']),(1,1))

    def test_old_model_or_changed_document_not_searched(self):
        rows=[paper('a'),paper('b')];self.idx.update(rows)
        rows[0]['abstract']='New content'
        self.idx.db.execute('UPDATE embeddings SET fingerprint=? WHERE paper_id=?',('other-model','b'))
        self.idx.db.commit()
        self.assertEqual(self.idx.valid(rows),{})

    def test_bad_dimensions_are_not_searched(self):
        rows=[paper('a')];self.idx.update(rows)
        self.idx.db.execute('UPDATE embeddings SET vector=?',(b'bad',));self.idx.db.commit()
        self.assertEqual(self.idx.valid(rows),{})

    def test_default_excludes_archived_and_title_only(self):
        rows=[paper('a'),paper('b',archived=True),paper('c',abstract='')]
        result=s.search(self.idx,'curriculum',rows,mode='keyword')
        self.assertEqual([p['id'] for p in result['results']],['a'])
        result=s.search(self.idx,'curriculum',rows,mode='keyword',coverage='all',scope='all')
        self.assertEqual(len(result['results']),3)

    def test_model_failure_has_visible_keyword_fallback(self):
        rows=[paper('a')]
        with patch.object(self.idx,'dense_scores',side_effect=RuntimeError('model unavailable')):
            result=s.search(self.idx,'curriculum',rows)
        self.assertEqual(result['effective_mode'],'keyword')
        self.assertIn('model unavailable',result['fallback_reason'])
        self.assertEqual(result['results'][0]['id'],'a')

    def test_scope_and_filters_applied_before_ranking(self):
        rows=[paper('a'),paper('b',archived=True)]
        self.assertEqual(s.search(self.idx,'curriculum',rows,mode='keyword',scope='archive')['results'][0]['id'],'b')
        self.assertFalse(s.search(self.idx,'curriculum',rows,mode='keyword',source='missing')['results'])

    def test_new_archive_identity_is_seen_without_reindex(self):
        store=radar.Store(self.root/'candidates.sqlite')
        store.upsert(paper('a'),'test','https://example.org');store.close()
        target=self.root/'papers/local';target.mkdir(parents=True)
        (target/'metadata.json').write_text(json.dumps({'identifiers':{'pmlr':'v202/a'}}))
        self.assertTrue(s.load_papers(self.root,self.root)[0]['archived'])

    def test_query_validation(self):
        for query in ['', 'x'*1001]:
            with self.assertRaises(ValueError):s.search(self.idx,query,[])

    def test_unicode_alias_lexical_baseline(self):
        result=s.search(self.idx,'课程', [paper('a')],mode='keyword')
        self.assertEqual(result['results'][0]['id'],'a')

    def test_local_lock_prevents_overlapping_embedding(self):
        with s.inference_slot():
            with self.assertRaisesRegex(RuntimeError,'正在使用'):
                self.idx.update([paper('a')])

    def test_query_cache_avoids_model_reload(self):
        try:import numpy
        except ImportError:self.skipTest('numpy optional')
        rows=[paper('a')];self.idx.update(rows)
        first=s.search(self.idx,'question',rows,mode='semantic')
        self.assertFalse(first['query_cache_hit'])
        with patch.object(self.idx,'encoder',side_effect=AssertionError('must reuse query cache')):
            second=s.search(self.idx,'question',rows,mode='semantic')
        self.assertTrue(second['query_cache_hit'])
        self.assertEqual(first['results'],second['results'])

    def test_bounded_index_can_resume(self):
        rows=[paper(str(i)) for i in range(5)]
        first=self.idx.update(rows,limit=2)
        self.assertEqual((first['encoded'],first['pending']),(2,3))
        self.assertEqual(self.idx.update(rows)['encoded'],3)
    def test_new_note_does_not_wait_behind_catalogue_backfill(self):
        rows=[paper('title',abstract=''),paper('abstract'),dict(paper('note'),kind='note')]
        self.idx.update(rows,limit=1)
        self.assertEqual(set(self.idx.valid(rows)),{'note'})
        self.idx.update(rows,limit=1)
        self.assertEqual(set(self.idx.valid(rows)),{'note','abstract'})

    def test_notes_have_source_locations_and_stay_out_of_daily_candidates(self):
        p=paper('a',archived=True);p['archive_id']='local'
        directory=self.root/'papers/local';directory.mkdir(parents=True)
        (directory/'metadata.json').write_text(json.dumps({'artifacts':{'notes':'notes.md'}}))
        (directory/'notes.md').write_text('# Learning progress\n'+('Notes discuss curriculum and uncertainty. '*6)+'\n## Limitations\n'+('This is a teaching explanation, not original evidence. '*4))
        passages=s.load_notes([p],self.root)
        self.assertEqual(len(passages),2)
        self.assertEqual(passages[0]['note_location']['line_start'],1)
        self.assertIn('AI',passages[0]['note_evidence'])
        self.assertEqual(s.search(self.idx,'curriculum',passages,mode='keyword')['results'],[])
        self.assertTrue(s.search(self.idx,'curriculum',passages,mode='keyword',scope='notes')['results'])

    def test_notes_cannot_escape_the_archive(self):
        p=paper('a',archived=True);p['archive_id']='local'
        directory=self.root/'papers/local';directory.mkdir(parents=True)
        (directory/'metadata.json').write_text(json.dumps({'artifacts':{'notes':'../../private.md'}}))
        (self.root/'private.md').write_text('not an archive note '*10)
        self.assertEqual(s.load_notes([p],self.root),[])


if __name__=='__main__':unittest.main()
