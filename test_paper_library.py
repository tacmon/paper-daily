"""Offline regression checks. Fixtures are intentionally synthetic, not real media."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import paper_library as lib


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".archive-test-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def register(self, **changes):
        values = dict(doi=None, arxiv="1705.06366", pmlr="v80/florensa18a", id=None,
                      date="2026-11-17", title="GoalGAN", author=["Carlos Florensa"],
                      year=2018, allow_repeat=False)
        values.update(changes)
        return lib.register(self.root, argparse.Namespace(**values))

    def load(self, paper_id="arxiv-1705.06366"):
        return lib.read_json(self.root / "papers" / paper_id / "metadata.json")

    def save(self, data):
        lib.atomic_json(self.root / "papers" / data["id"] / "metadata.json", data)

    def test_shanghai_day_boundary(self):
        self.assertEqual(lib.today(datetime(2026, 11, 16, 15, 59, tzinfo=timezone.utc)), "2026-11-16")
        self.assertEqual(lib.today(datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)), "2026-11-17")
        with self.assertRaises(ValueError):
            lib.today(datetime(2026, 11, 16))

    def test_identifier_normalization_and_reuse(self):
        first = self.register()
        second = self.register(arxiv="https://arxiv.org/pdf/1705.06366v2.pdf", pmlr="https://proceedings.mlr.press/v80/florensa18a/florensa18a.pdf", doi="https://doi.org/10.48550/arXiv.1705.06366")
        third = self.register(arxiv=None, pmlr=None, doi="doi:10.48550/ARXIV.1705.06366")
        self.assertEqual(first["paper_id"], second["paper_id"])
        self.assertEqual(first["paper_id"], third["paper_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(list((self.root / "papers").glob("*/metadata.json"))), 1)
        with self.assertRaises(ValueError):
            self.register(date="2026-11-18")
        with self.assertRaises(ValueError):
            self.register(arxiv="1707.05300", pmlr=None)

    def test_duplicate_canonical_identifiers_are_reported(self):
        self.register()
        self.register(arxiv="1707.05300", pmlr=None, date="2026-11-18")
        data = self.load("arxiv-1707.05300")
        data["identifiers"]["arxiv"] = "1705.06366v3"
        self.save(data)
        result = lib.validate(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("duplicate arxiv" in item for item in result["errors"]))

    def test_pmlr_volume_is_part_of_identity(self):
        one = self.register(arxiv=None, pmlr="https://proceedings.mlr.press/v130/chen21a.html")
        two = self.register(arxiv=None, pmlr="https://proceedings.mlr.press/v139/chen21a.html", date="2026-11-18")
        self.assertNotEqual(one["paper_id"], two["paper_id"])
        with self.assertRaises(ValueError):
            lib.canonical_id("pmlr", "chen21a")

    def test_missing_source_records_are_errors(self):
        (self.root / "papers/orphan").mkdir(parents=True)
        (self.root / "days/2026-11-17").mkdir(parents=True)
        result = lib.validate(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("papers/orphan/metadata.json" in error for error in result["errors"]))
        self.assertTrue(any("days/2026-11-17/paper.json" in error for error in result["errors"]))

    def test_preexisting_user_symlink_is_not_taken_over(self):
        self.register()
        data = self.load()
        link = self.root / f"indexes/authors/{lib.author_key('Carlos Florensa')}/{data['id']}"
        link.parent.mkdir(parents=True)
        link.symlink_to("../../../papers/" + data["id"], target_is_directory=True)
        lib.rebuild(self.root)
        data["authors"] = [{"name": "Another Author"}]
        self.save(data)
        lib.rebuild(self.root)
        self.assertTrue(link.is_symlink())

    def test_only_verified_archived_citations_have_relative_links(self):
        self.register()
        self.register(arxiv="1707.05300", pmlr=None, date="2026-11-18")
        data = self.load()
        data["citations"] = [
            {"target_id": "arxiv-1707.05300", "relation": "cites", "verified": True,
             "evidence": {"source": "paper.pdf", "locator": "test fixture only"}},
            {"target_id": "not-archived", "relation": "cites", "verified": True,
             "evidence": {"source": "paper.pdf", "locator": "test fixture only"}},
            {"target_id": "unverified", "relation": "cites", "verified": False},
        ]
        self.save(data)
        lib.rebuild(self.root)
        links = lib.expected_links(lib.load_library(self.root)[0])
        self.assertEqual(len(links), 3)  # two author links plus one verified archived citation
        for relative, target in links.items():
            link = self.root / relative
            self.assertTrue(link.is_symlink())
            self.assertFalse(link.readlink().is_absolute())
            self.assertEqual(link.resolve(), (self.root / target).resolve())
        self.assertFalse((self.root / "indexes/citations/arxiv-1705.06366/not-archived").exists())

    def test_missing_video_never_ready(self):
        self.register()
        data = self.load()
        base = self.root / "papers" / data["id"]
        for key, relative in data["artifacts"].items():
            path = base / relative
            if key == "pdf":
                path.write_bytes(b"%PDF-1.7\n synthetic test fixture")
            elif key == "slides":
                with zipfile.ZipFile(path, "w") as deck:
                    deck.writestr("ppt/presentation.xml", "<test/>")
            elif key != "video":
                path.write_text("synthetic test fixture", encoding="utf-8")
        data["verification"] = {**{key: True for key in lib.CHECKS}, "video_duration_seconds": 660}
        data["status"] = "ready"
        self.save(data)
        self.assertEqual(lib.effective_status(self.root, data), "partial")
        self.assertFalse(lib.validate(self.root)["ok"])
        (base / data["artifacts"]["video"]).write_bytes(b"synthetic test fixture")
        self.assertEqual(lib.effective_status(self.root, data), "ready")
        data["verification"]["video_duration_seconds"] = 599
        self.assertEqual(lib.effective_status(self.root, data), "partial")
        data["verification"]["video_duration_seconds"] = 660
        data["verification"]["video_audio_checked"] = False
        self.assertEqual(lib.effective_status(self.root, data), "partial")

    def test_generated_links_cleaned_but_user_files_preserved(self):
        self.register()
        lib.rebuild(self.root)
        data = self.load()
        old_link = self.root / f"indexes/authors/{lib.author_key('Carlos Florensa')}/{data['id']}"
        self.assertTrue(old_link.is_symlink())
        data["authors"] = [{"name": "Another Author"}]
        self.save(data)
        lib.rebuild(self.root)
        self.assertFalse(old_link.is_symlink())
        user_file = self.root / f"indexes/authors/{lib.author_key('New Author')}/{data['id']}"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text("keep this user file", encoding="utf-8")
        data["authors"] = [{"name": "New Author"}]
        self.save(data)
        result = lib.rebuild(self.root)
        self.assertEqual(user_file.read_text(), "keep this user file")
        self.assertTrue(result["warnings"])
        (self.root / "indexes/broken").symlink_to("missing")
        self.assertTrue(any("broken link" in error for error in lib.validate(self.root)["errors"]))

    def test_path_escape_and_bad_day_rejected(self):
        self.register()
        data = self.load()
        data["artifacts"]["video"] = "../../outside.mp4"
        data["status"] = "ready"
        self.save(data)
        self.assertTrue(any("escapes" in error for error in lib.validate(self.root)["errors"]))
        with self.assertRaises(ValueError):
            self.register(date="2026-02-30")

    def test_no_unverified_claim_of_acceptance(self):
        self.register()
        result = lib.validate(self.root)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])
        self.assertFalse(lib.validate(self.root, require_ready=True)["ok"])
        lib.rebuild(self.root)
        self.assertIn("partial", (self.root / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
