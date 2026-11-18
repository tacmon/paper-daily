import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from scripts import obsidian_bridge as bridge


class ObsidianBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root/'papers/paper-a'
        self.note = self.base/'revisions/v3/notes.md'
        self.note.parent.mkdir(parents=True)
        self.note.write_text('My notes: $\\theta$\n')
        self.paper = {'id':'paper-a','title':'Title','title_zh':'中文标题', 'status':'partial',
                      'artifacts':{'notes':'revisions/v3/notes.md'}}
        self.item = {'data':self.paper,'base':self.base}
        self.days = {'2026-11-18':{'paper_id':'paper-a'}}

    def tearDown(self):
        self.temp.cleanup()

    def test_uri_targets_current_revision(self):
        uri = bridge.note_uri(self.item, self.root)
        self.assertEqual(urlsplit(uri).scheme,'obsidian')
        self.assertEqual(parse_qs(urlsplit(uri).query)['path'],[str(self.note)])
        self.assertEqual(parse_qs(urlsplit(uri).query)['paneType'],['tab'])

    def test_cannot_open_outside_paper(self):
        self.paper['artifacts']['notes']='../../../private.md'
        with self.assertRaises(ValueError):bridge.note_uri(self.item,self.root)

    def test_sync_tracks_revision_without_rewriting_notes(self):
        before = self.note.read_bytes()
        bridge.refresh_index(self.root,{'paper-a':self.paper},self.days)
        updated=self.base/'revisions/v4/notes.md';updated.parent.mkdir();updated.write_text('New version')
        self.paper['artifacts']['notes']='revisions/v4/notes.md'
        result=bridge.refresh_index(self.root,{'paper-a':self.paper},self.days)
        self.assertTrue(result['updated'])
        self.assertIn('revisions/v4/notes.md',(self.root/bridge.INDEX).read_text())
        self.assertNotIn('revisions/v3/notes.md',(self.root/bridge.INDEX).read_text())
        self.assertEqual(self.note.read_bytes(),before)

    def test_preserves_hand_edited_navigation(self):
        bridge.refresh_index(self.root,{'paper-a':self.paper},self.days)
        target=self.root/bridge.INDEX;target.write_text(target.read_text()+'My annotation\n')
        result=bridge.refresh_index(self.root,{'paper-a':self.paper},self.days)
        self.assertFalse(result['updated']);self.assertTrue(target.read_text().endswith('My annotation\n'))

    def test_unregistered_vault_does_not_launch_wrong_library(self):
        with patch.object(bridge,'registered',return_value=False), patch.object(bridge.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'打开已有文件夹'):bridge.open_note(self.item,self.root)
            run.assert_not_called()


if __name__=='__main__':unittest.main()
