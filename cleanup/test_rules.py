"""Run with: python3 -m unittest cleanup.test_rules  (examples are real tags from the library)"""
import unittest

from cleanup.rules import clean_album, clean_date, clean_title, normalize, safe_name, split_artists


class Artists(unittest.TestCase):
    cases = {
        "Eminem feat. Dr. Dre, Snoop Dogg, Xzibit and Nate Dogg": ["Eminem", "Dr. Dre", "Snoop Dogg", "Xzibit", "Nate Dogg"],
        "BABYMETAL feat. Tim Henson and Scott LePage": ["BABYMETAL", "Tim Henson", "Scott LePage"],
        "Bruno Mars feat. Cee Lo Green & B.o.B": ["Bruno Mars", "Cee Lo Green", "B.o.B"],
        "Hozier featuring Mavis Staples": ["Hozier", "Mavis Staples"],
        "Eminem feat. Bizarre from D‐12": ["Eminem", "Bizarre from D-12"],
        "A. R. Rahman, Javed Ali, Mohit Chauhan & Nizami Brothers": ["A. R. Rahman", "Javed Ali", "Mohit Chauhan", "Nizami Brothers"],
        "Pritam/ KK": ["Pritam", "KK"],
        "Jay-Z / Linkin Park": ["Jay-Z", "Linkin Park"],
        "Arijit Singh;Atif Aslam;Yo Yo Honey Singh": ["Arijit Singh", "Atif Aslam", "Yo Yo Honey Singh"],
        "KPop Demon Hunters Cast, HUNTR/X & Saja Boys": ["KPop Demon Hunters Cast", "HUNTR/X", "Saja Boys"],
        "HUNTR/X": ["HUNTR/X"],
        "Vishal-Shekhar, Shaan": ["Vishal-Shekhar", "Shaan"],
        "Salim-Sulaiman": ["Salim-Sulaiman"],
        "Vishal & Shekhar, Piyush Mishra & Sunidhi Chauhan": ["Vishal & Shekhar", "Piyush Mishra", "Sunidhi Chauhan"],
        "Fitz and The Tantrums": ["Fitz and The Tantrums"],
        "Band Master Rangeela And Rasila": ["Band Master Rangeela And Rasila"],
        "Jorge Rivera-Herrans, Cast of EPIC: The Musical": ["Jorge Rivera-Herrans", "Cast of EPIC: The Musical"],
        "Mohit Chauhan & Suzanne D’Mello": ["Mohit Chauhan", "Suzanne D'Mello"],
        "Earle Gresham Jr.": ["Earle Gresham Jr."],
        "System Of A Down": ["System Of A Down"],
    }

    def test_split(self):
        for raw, expected in self.cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(split_artists(raw), expected)

    def test_multivalue_list(self):
        self.assertEqual(split_artists(["Skrillex", "Sirah"]), ["Skrillex", "Sirah"])
        self.assertEqual(split_artists(["Eminem", "eminem & Dr. Dre"]), ["Eminem", "Dr. Dre"])


class Titles(unittest.TestCase):
    cases = {
        "Middle (Feat. Bipolar Sunshine)": ("Middle", ["Bipolar Sunshine"]),
        "The Half (Feat. Young Thung, Jeremih & Swizz Beatz)": ("The Half", ["Young Thung", "Jeremih", "Swizz Beatz"]),
        "Dusk Till Dawn (feat. Sia) (Radio Edit)": ("Dusk Till Dawn (Radio Edit)", ["Sia"]),
        "The Hot Wind Blowing (Platinum mix) feat. Ferry Corsten": ("The Hot Wind Blowing (Platinum mix)", ["Ferry Corsten"]),
        "Bitter Sweet Symphony (Remastered 2016)": ("Bitter Sweet Symphony", []),
        "The Man Who Sold The World (2010 Digital Remaster)": ("The Man Who Sold The World", []),
        "Heroes - 2017 Remaster": ("Heroes", []),
        "Public Service Announcement 2000": ("Public Service Announcement 2000", []),
        "Would You Fall in Love with Me Again": ("Would You Fall in Love with Me Again", []),
        "Main Agar Kahoon (From \"Om Shanti Om\")": ("Main Agar Kahoon (From \"Om Shanti Om\")", []),
        "Aadat (From  Kalyug )": ("Aadat (From Kalyug)", []),
        "Don’t Stop": ("Don't Stop", []),
    }

    def test_clean_title(self):
        for raw, expected in self.cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), expected)


class Albums(unittest.TestCase):
    cases = {
        "Urban Hymns (Remastered 2016)": "Urban Hymns",
        "Exile - EP": "Exile EP",
        "Ma Meilleure Ennemie (from the series Arcane League of Legends) - Single": "Ma Meilleure Ennemie (from the series Arcane League of Legends)",
        "Night Visions [2013]": "Night Visions",
        "2013 - Random Access Memories": "Random Access Memories",
        "[2014] Ek Villain": "Ek Villain",
        "Nectar [FLAC]": "Nectar",
        "METAL FORTH [FLAC] [24B-44.1kHz]": "METAL FORTH",
        "OK Computer {24-96}": "OK Computer",
        "(III)": "(III)",
        "Hybrid Theory (20th Anniversary Edition)": "Hybrid Theory (20th Anniversary Edition)",
        "Delhi‐6": "Delhi-6",
        "1989 (Taylor's Version)": "1989 (Taylor's Version)",
    }

    def test_clean_album(self):
        for raw, expected in self.cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_album(raw), expected)


class Misc(unittest.TestCase):
    def test_date(self):
        self.assertEqual(clean_date("2014-00-00"), "2014")
        self.assertEqual(clean_date("2009-01-02"), "2009-01-02")

    def test_safe_name(self):
        self.assertEqual(safe_name("Why This Kolaveri Di? (The Soup of Love)"), "Why This Kolaveri Di (The Soup of Love)")
        self.assertEqual(safe_name("EPIC: The Troy Saga"), "EPIC - The Troy Saga")
        self.assertEqual(safe_name("The Days / Nights EP"), "The Days - Nights EP")

    def test_normalize(self):
        self.assertEqual(normalize("King For A Day , Shout"), "King For A Day, Shout")


if __name__ == "__main__":
    unittest.main()
