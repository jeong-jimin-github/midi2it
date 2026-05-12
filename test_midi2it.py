import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import mido

from midi2it import encode_it_text, write_it, convert_midi_to_it


class EncodeItTextTests(unittest.TestCase):
    def test_replaces_non_latin_characters(self):
        self.assertEqual(encode_it_text("한글abc", 6), b"??abc\x00")

    def test_truncates_long_strings(self):
        self.assertEqual(encode_it_text("abcdefgh", 5), b"abcde")

    def test_pads_short_strings(self):
        self.assertEqual(encode_it_text("abc", 5), b"abc\x00\x00")


class TempoTests(unittest.TestCase):
    def test_write_it_uses_initial_tempo(self):
        with tempfile.NamedTemporaryFile(suffix=".it", delete=False) as tmp:
            it_path = tmp.name

        try:
            write_it(
                it_path,
                "tempo-test",
                samples=[{"name": "sample", "data": b"\x00\x00"}],
                patterns=[b"\x00"],
                orders=[0],
                initial_tempo=90,
            )
            with open(it_path, "rb") as f:
                data = f.read(64)
            self.assertEqual(data[51], 90)
        finally:
            Path(it_path).unlink(missing_ok=True)

    def test_convert_midi_to_it_passes_midi_bpm_to_writer(self):
        with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as midi_tmp:
            midi_path = midi_tmp.name
        with tempfile.NamedTemporaryFile(suffix=".it", delete=False) as out_tmp:
            out_path = out_tmp.name

        try:
            mid = mido.MidiFile(ticks_per_beat=480)
            track = mido.MidiTrack()
            mid.tracks.append(track)
            track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90), time=0))
            track.append(mido.Message("note_on", note=60, velocity=100, time=0, channel=0))
            mid.save(midi_path)

            class FakeFluidSynth:
                def __init__(self, sf2_path):
                    self.sf2_path = sf2_path

                def render_sample(self, bank, prog, note=60, duration_sec=1.0):
                    return b"\x00\x00"

            with patch("midi2it.FluidSynth", FakeFluidSynth), patch("midi2it.write_it") as mock_write:
                convert_midi_to_it(midi_path, "dummy.sf2", out_path)

            self.assertEqual(mock_write.call_args.kwargs["initial_tempo"], 90)
        finally:
            Path(midi_path).unlink(missing_ok=True)
            Path(out_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
