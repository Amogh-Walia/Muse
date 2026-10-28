"""Run with: .venv/bin/python -m unittest lyrics.test_files"""
import os
import tempfile
import unittest

import crud
from lyrics import files


class Parsing(unittest.TestCase):
    def test_synced_detection(self):
        self.assertTrue(files.is_synced("[ar:Someone]\n[00:12.34] First line\n"))
        self.assertFalse(files.is_synced("Just words\n[Chorus]\nMore words"))

    def test_parse_offset_and_repeated_stamps(self):
        text = "[offset:+500]\n[00:01.00][00:20.50] Chorus\n[00:10.00] Verse\n[length: 03:00]\n"
        self.assertEqual(files.parse(text), [(0.5, "Chorus"), (9.5, "Verse"), (20.0, "Chorus")])


class Finding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "A", "Album"))
        os.makedirs(os.path.join(self.root, "Old"))
        self.conn = crud.connect(os.path.join(self.root, "muse.db"))
        for path, title in (("A/Album/01 - One.flac", "One"), ("A/Album/02 - Two.flac", "Two"),
                            ("A/Album/03 - Three.flac", "Three")):
            open(os.path.join(self.root, path), "w").close()
            crud.tracks.upsert(self.conn, {"path": path, "title": title}, {})
        self.write("A/Album/01 - One.txt", "plain words")
        self.write("A/Album/01 - One.lrc", "[00:01.00] synced words")
        self.write("A/Album/02 - Two.txt", "only plain")
        self.write("Old/03 Three.lrc", "[00:01.00] orphan that fits Three")
        self.write("Old/01 - One.lrc", "[00:01.00] orphan of One, which has lyrics")
        self.write("Old/99 - Nobody.lrc", "[00:01.00] fits nothing")

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(text)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_synced_wins_and_status(self):
        best = files.best(self.root, "A/Album/01 - One.flac", embedded=["embedded plain"])
        self.assertEqual((best["source"], best["synced"]), ("lrc", True))
        statuses = files.status_for_all(self.conn, self.root)
        self.assertCountEqual(statuses.values(), [(None, None), ("plain", "txt"), ("synced", "lrc")])

    def test_orphans(self):
        crud.lyrics.set_status(self.conn, files.status_for_all(self.conn, self.root))
        found = {o["path"]: (o["action"], o["target"]) for o in files.orphans(self.conn, self.root)}
        self.assertEqual(found, {
            "Old/03 Three.lrc": ("reattach", "A/Album/03 - Three.lrc"),
            "Old/01 - One.lrc": ("trash", None),
            "Old/99 - Nobody.lrc": ("trash", None),
        })


if __name__ == "__main__":
    unittest.main()
