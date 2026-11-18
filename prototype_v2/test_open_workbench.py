"""Check launch boundaries without touching the user's browser."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import open_workbench as workbench


class WorkbenchEntryTests(unittest.TestCase):
    def test_journal_entry_selects_current_paper(self):
        with patch.object(workbench,'ensure',return_value=workbench.URL):
            self.assertEqual(workbench.destination(journal='paper-a'),workbench.URL+'reading-log?paper=paper-a')
            with self.assertRaises(ValueError):workbench.destination(journal='../config')
    def test_notes_entry_starts_service_and_selects_notes(self):
        with patch.object(workbench, 'ensure', return_value=workbench.URL) as ensure:
            self.assertEqual(workbench.destination('notes'), workbench.URL+'#scope=notes')
            ensure.assert_called_once()

    def test_local_notes_do_not_need_service_and_cannot_escape_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            note = root/'papers/example/notes.html'
            note.parent.mkdir(parents=True); note.write_text('<h1>Notes</h1>')
            with patch.object(workbench, 'HERE', root/'prototype_v2'), patch.object(workbench, 'ensure') as ensure:
                self.assertEqual(workbench.destination(note=note), note.as_uri())
                ensure.assert_not_called()
                with self.assertRaises(ValueError):
                    workbench.destination(note=root/'config.html')
                with self.assertRaises(FileNotFoundError):
                    workbench.destination(note=note.parent/'missing.html')

    def test_browser_uses_dedicated_profile_and_reports_launch_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)/'isolated-profile'
            browser = Path(folder)/'browser'; browser.touch()
            with patch.object(workbench, 'CHROME', browser), patch.object(workbench, 'PROFILE', profile), patch.object(workbench.subprocess, 'Popen') as popen:
                popen.return_value.wait.side_effect = subprocess.TimeoutExpired('chrome', .4)
                workbench.launch_browser(workbench.URL)
                args = popen.call_args.args[0]
                self.assertIn('--user-data-dir='+str(profile), args)
                self.assertIn('--disable-sync', args)
                self.assertEqual(args[-1], workbench.URL)
                popen.return_value.wait.side_effect = None
                popen.return_value.wait.return_value = 1
                with self.assertRaises(RuntimeError):
                    workbench.launch_browser(workbench.URL)


if __name__ == '__main__':
    unittest.main()
