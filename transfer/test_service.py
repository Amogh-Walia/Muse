import os
import tempfile
import unittest

from transfer import service


class SafeNameTest(unittest.TestCase):
    def test_characters_usb_sticks_reject(self):
        self.assertEqual(service.safe_name("Delhi Belly: Original Motion Picture Soundtrack"),
                         "Delhi Belly - Original Motion Picture Soundtrack")
        self.assertEqual(service.safe_name('Who Are You?'), "Who Are You")
        self.assertEqual(service.safe_name('12" Mix <Live> *'), "12' Mix (Live)")
        self.assertEqual(service.safe_name("AC|DC"), "AC-DC")

    def test_trailing_dots_and_spaces(self):
        self.assertEqual(service.safe_name("Vol. 2..."), "Vol. 2")
        self.assertEqual(service.safe_name("???"), "_")

    def test_ordinary_names_unchanged(self):
        for name in ("01 - Song.flac", "Sigur Rós", "2008 - Crystal Castles"):
            self.assertEqual(service.safe_name(name), name)


class InsideTest(unittest.TestCase):
    def test_inside(self):
        with tempfile.TemporaryDirectory() as d:
            lib, other = os.path.join(d, "Music"), os.path.join(d, "Music2")
            os.makedirs(os.path.join(lib, "A"))
            os.makedirs(other)
            self.assertTrue(service._inside(os.path.join(lib, "A"), lib))
            self.assertTrue(service._inside(lib, lib))
            self.assertFalse(service._inside(other, lib))  # a shared prefix isn't containment
            self.assertTrue(service._inside(lib, d))


if __name__ == "__main__":
    unittest.main()
