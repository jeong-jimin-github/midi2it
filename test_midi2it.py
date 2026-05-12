import unittest

from midi2it import encode_it_text


class EncodeItTextTests(unittest.TestCase):
    def test_replaces_non_latin_characters(self):
        self.assertEqual(encode_it_text("한글abc", 6), b"??abc\x00")

    def test_truncates_long_strings(self):
        self.assertEqual(encode_it_text("abcdefgh", 5), b"abcde")

    def test_pads_short_strings(self):
        self.assertEqual(encode_it_text("abc", 5), b"abc\x00\x00")


if __name__ == "__main__":
    unittest.main()
