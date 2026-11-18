import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.init_library import initialize, ROOT
import paper_library
from scripts.paper_notify import available_papers


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ['config.example.json', 'reading_profile.example.md', 'pipeline_prompt.example.md',
                     'prototype_v2/sources.example.json', 'prototype_v2/research_queries.example.json']:
            dest = self.root/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/name, dest)

    def tearDown(self):
        self.temp.cleanup()

    def run_init(self):
        with contextlib.redirect_stdout(io.StringIO()):
            initialize(self.root)

    def test_empty_install_is_valid_without_publishing_a_fake_report(self):
        self.run_init()
        self.assertTrue(paper_library.validate(self.root)['ok'])
        self.assertEqual(available_papers(self.root), [])
        self.assertTrue((self.root/'prototype_v2/index.html').is_file())
        self.assertTrue((self.root/'reading_log.md').is_file())

    def test_reinitializing_keeps_personal_settings_and_existing_workbench(self):
        self.run_init()
        for name in ['reading_profile.md', 'reading_log.md', 'config.json', 'prototype_v2/index.html']:
            (self.root/name).write_text('personal content to preserve')
        self.run_init()
        for name in ['reading_profile.md', 'reading_log.md', 'config.json', 'prototype_v2/index.html']:
            self.assertEqual((self.root/name).read_text(), 'personal content to preserve')
