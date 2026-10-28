"""Run with: .venv/bin/python -m unittest cleanup.test_retention"""
import datetime
import json
import os
import tempfile
import unittest

import config
import crud
from cleanup import retention


class Expiry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.root, self.journals, self.cache = (os.path.join(base, d) for d in ("music", "journal", "cache"))
        for d in (self.root, self.journals, self.cache):
            os.makedirs(d)
        self._saved = retention.JOURNAL_DIR, config.ART_CACHE
        retention.JOURNAL_DIR, config.ART_CACHE = self.journals, self.cache
        self.conn = crud.connect(os.path.join(base, "muse.db"))
        crud.set_root(self.conn, self.root)

        def journal(name, backup=None):
            with open(os.path.join(self.journals, name), "w") as f:
                json.dump({"root": self.root, "entries": [{"op": "tags"}], **({"backup": backup} if backup else {})}, f)
        journal("20260101-120000.json")                                    # old: expires
        os.makedirs(os.path.join(self.root, ".muse-trash", "20260101-120000"))
        with open(os.path.join(self.root, ".muse-trash", "20260101-120000", "dupe.flac"), "wb") as f:
            f.write(b"x" * 1000)
        journal("20260301-120000-000001-artwork.json", backup="20260301-120000-000001-artwork-backup")  # recent: kept
        os.makedirs(os.path.join(self.journals, "20260301-120000-000001-artwork-backup"))
        journal("20260302-120000.undone.json")                             # undone: removed whatever its age
        os.makedirs(os.path.join(self.journals, "20260201-000000-orphan-backup"))  # no journal: removed
        with open(os.path.join(self.cache, "a" * 40), "wb") as f:         # nothing refers to it: removed
            f.write(b"img")

    def tearDown(self):
        retention.JOURNAL_DIR, config.ART_CACHE = self._saved
        self.conn.close()
        self.tmp.cleanup()

    def test_prune(self):
        now = datetime.datetime(2026, 3, 10)
        inv = retention.inventory(self.root, days=30, now=now)
        self.assertEqual([j["expired"] for j in inv["journals"]], [True, False, False])
        removed = retention.prune(self.conn, self.root, days=30, now=now)
        self.assertEqual(removed["expired_journals"], 1)
        self.assertEqual(removed["undone_journals"], 1)
        self.assertEqual(removed["orphan_backups"], 1)
        self.assertEqual(removed["trash_files"], 1)
        self.assertEqual(removed["cached_covers"], 1)
        self.assertEqual(sorted(os.listdir(self.journals)),
                         ["20260301-120000-000001-artwork-backup", "20260301-120000-000001-artwork.json"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".muse-trash")))

    def test_zero_days_keeps_everything_but_dead_weight(self):
        removed = retention.prune(self.conn, self.root, days=0, now=datetime.datetime(2030, 1, 1))
        self.assertNotIn("expired_journals", removed)
        self.assertNotIn("trash_files", removed)
        self.assertEqual(removed["undone_journals"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.root, ".muse-trash", "20260101-120000", "dupe.flac")))


if __name__ == "__main__":
    unittest.main()
