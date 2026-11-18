"""Notification acceptance tests. Media fixtures are deliberately synthetic."""
import importlib.util
import argparse
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('paper_notify', ROOT/'scripts/paper_notify.py')
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class Notifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        notify.library.register(self.root, argparse.Namespace(doi=None, arxiv='1705.06366', pmlr=None, id=None, date=notify.library.today(), title='Synthetic test paper', author=['Example Author'], year=2018, allow_repeat=False))
        self.data = json.loads((self.root/'papers/arxiv-1705.06366/metadata.json').read_text())
        self.data['status']='ready'
        self.data['verification']={**{key:True for key in notify.library.CHECKS}, 'video_duration_seconds':660}
        self.paper = self.root/'papers'/self.data['id']
        self.paper.mkdir(parents=True,exist_ok=True)
        for key, rel in self.data['artifacts'].items():
            p = self.paper/rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if key == 'slides':
                with zipfile.ZipFile(p, 'w') as archive:
                    archive.writestr('ppt/presentation.xml', '<fixture/>')
            else:
                p.write_bytes(b'%PDF-synthetic-fixture' if key == 'pdf' else b'synthetic-fixture')
        (self.paper/'background.md').write_text('Synthetic source evidence fixture')
        self.save()
        self.day = notify.library.today()
        (self.root/'days'/self.day).mkdir(parents=True,exist_ok=True)
        (self.root/'days'/self.day/'paper.json').write_text(json.dumps({'date':self.day, 'timezone':'Asia/Shanghai', 'paper_id':self.data['id']}))

    def save(self):
        (self.paper/'metadata.json').write_text(json.dumps(self.data))

    def tearDown(self):
        self.temp.cleanup()

    def test_ready_metadata_suffices_without_day_status(self):
        self.assertEqual(len(notify.pending_papers(self.root)), 1)

    def test_missing_video_does_not_announce_completion(self):
        (self.paper/self.data['artifacts']['video']).unlink()
        self.assertEqual(notify.pending_papers(self.root), [])

    def test_background_is_required_even_for_legacy_metadata(self):
        (self.paper/'background.md').unlink()
        self.assertEqual(notify.pending_papers(self.root), [])

    def test_partial_metadata_does_not_announce_completion(self):
        self.data['status']='partial'; self.save()
        self.assertEqual(notify.pending_papers(self.root), [])

    def test_shown_but_unacknowledged_is_recovered(self):
        item=notify.pending_papers(self.root)[0]
        notify.save_state(item, status='displayed', displayed_at='2026-11-17T00:00:00+08:00')
        self.assertEqual(len(notify.pending_papers(self.root)), 1)

    def test_acknowledgement_prevents_repeat_but_not_preview(self):
        item=notify.pending_papers(self.root)[0]
        notify.save_state(item, status='acknowledged', acknowledged_at='2026-11-17T00:00:00+08:00', action='dismissed')
        self.assertEqual(notify.pending_papers(self.root), [])
        self.assertEqual(len(notify.pending_papers(self.root, include_acknowledged=True)), 1)

    def test_manual_browse_keeps_dismissed_reports_and_their_receipts(self):
        item = notify.pending_papers(self.root)[0]
        notify.save_state(item, status='acknowledged', acknowledged_at='2026-11-17T00:00:00+08:00')
        receipt = item['state_path'].read_bytes()
        self.assertEqual(len(notify.available_papers(self.root)), 1)
        self.assertEqual(item['state_path'].read_bytes(), receipt)
        self.assertEqual(notify.pending_papers(self.root), [])

    def test_manual_browse_does_not_expose_future_ready_entries(self):
        tomorrow = (notify.datetime.fromisoformat(self.day) + timedelta(days=1)).date().isoformat()
        (self.root/'days'/self.day).rename(self.root/'days'/tomorrow)
        day = self.root/'days'/tomorrow/'paper.json'
        day.write_text(json.dumps({'date': tomorrow, 'timezone': 'Asia/Shanghai', 'paper_id': self.data['id']}))
        self.assertEqual(notify.available_papers(self.root), [])

    def test_manual_browse_does_not_consume_pending_notification(self):
        item = notify.pending_papers(self.root)[0]
        self.assertEqual(len(notify.available_papers(self.root)), 1)
        self.assertFalse(item['state_path'].exists())
        self.assertEqual(len(notify.pending_papers(self.root)), 1)

    def test_one_bad_paper_does_not_block_other_ready_papers(self):
        bad = dict(self.data, id='arxiv-1707.05300', artifacts=None,
                   identifiers={'arxiv':'1707.05300'})
        paper = self.root/'papers'/bad['id']; paper.mkdir()
        (paper/'metadata.json').write_text(json.dumps(bad))
        day = self.root/'days/2000-01-01'; day.mkdir()
        (day/'paper.json').write_text(json.dumps({'date':'2000-01-01', 'timezone':'Asia/Shanghai', 'paper_id':bad['id']}))
        self.assertEqual([i['data']['id'] for i in notify.pending_papers(self.root)], [self.data['id']])

    def test_damaged_notification_state_is_pending_not_lost(self):
        item = notify.pending_papers(self.root)[0]
        item['state_path'].parent.mkdir()
        item['state_path'].write_text('{unfinished')
        self.assertEqual(len(notify.pending_papers(self.root)), 1)


if __name__ == '__main__':
    unittest.main()
