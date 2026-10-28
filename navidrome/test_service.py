"""Run with: .venv/bin/python -m unittest navidrome.test_service"""
import os
import tempfile
import unittest
from unittest import mock

import crud
from navidrome import service


class Linking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = crud.connect(os.path.join(self.tmp.name, "muse.db"))
        rows = [
            ("Artist/2014 - Album/01 - One.flac", "Album", 1, 1, "One"),
            ("Artist/2014 - Album/02 - Two.flac", "Album", 1, 2, "Two"),
            ("Artist/2014 - Album/03 - Three.mp3", "Album", 1, 3, "Three"),
            ("Other/2001 - Live/04 - Four (live).flac", "Live", 1, 4, "Four (live)"),
        ]
        for path, album, disc, track, title in rows:
            crud.tracks.upsert(self.conn, {"path": path, "album": album, "disc_no": disc, "track_no": track, "title": title}, {})
        two = self.conn.execute("SELECT id FROM tracks WHERE title = 'Two'").fetchone()[0]
        self.conn.execute("INSERT INTO tags VALUES (?, 'MUSICBRAINZ_TRACKID', 'rec-two')", (two,))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_link_by_path_musicbrainz_and_tags(self):
        songs = [
            {"id": "n1", "albumId": "a1", "path": "/srv/music/Artist/2014 - Album/01 - One.flac"},           # real path
            {"id": "n2", "albumId": "a1", "path": "Artist/Album/01-02 - Two.flac", "musicBrainzId": "rec-two"},  # fake path
            {"id": "n3", "albumId": "a1", "path": "/music/Artist/2014 - Album/03 - Three.mp3"},              # docker mount
            {"id": "n4", "albumId": "a2", "path": "x/y.flac", "album": "LIVE", "discNumber": 1, "track": 4,
             "title": "Four (Live)"},                                                                       # tags only
            {"id": "n5", "albumId": "a3", "path": "Nowhere/song.flac", "title": "Not in Muse"},
        ]
        with mock.patch.object(service.client, "all_songs", return_value=iter(songs)):
            result = service.link(self.conn, "/srv/music")
        self.assertEqual(result["linked"], 4)
        self.assertEqual(result["navidrome_only"], 1)
        self.assertEqual(result["matched_by"], {"path": 2, "musicbrainz": 1, "tags": 1})
        ids = dict(self.conn.execute("SELECT title, navidrome_id FROM tracks"))
        self.assertEqual(ids, {"One": "n1", "Two": "n2", "Three": "n3", "Four (live)": "n4"})


if __name__ == "__main__":
    unittest.main()
