import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import mido
import numpy as np

from midi2it import encode_it_text, write_it, convert_midi_to_it, FluidSynth


class EncodeItTextTests(unittest.TestCase):
    def test_replaces_non_latin_characters(self):
        self.assertEqual(encode_it_text("한글abc", 6), b"??abc\x00")

    def test_truncates_long_strings(self):
        self.assertEqual(encode_it_text("abcdefgh", 5), b"abcde")

    def test_pads_short_strings(self):
        self.assertEqual(encode_it_text("abc", 5), b"abc\x00\x00")


class TempoTests(unittest.TestCase):
    def test_write_it_uses_max_mix_volume(self):
        with tempfile.NamedTemporaryFile(suffix=".it", delete=False) as tmp:
            it_path = tmp.name

        try:
            write_it(
                it_path,
                "volume-test",
                samples=[{"name": "sample", "data": b"\x00\x00"}],
                patterns=[b"\x00"],
                orders=[0],
            )
            with open(it_path, "rb") as f:
                data = f.read(64)
            self.assertEqual(data[49], 128)
        finally:
            Path(it_path).unlink(missing_ok=True)

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


class FluidSynthRenderSampleTests(unittest.TestCase):
    class FakeFS:
        def __init__(self, left_value=1000, right_value=1000):
            self.left_value = left_value
            self.right_value = right_value
            self.noteon_velocity = None

        def fluid_synth_program_select(self, synth, chan, sfid, bank, prog):
            return 0

        def fluid_synth_noteon(self, synth, chan, note, velocity):
            self.noteon_velocity = velocity
            return 0

        def fluid_synth_write_s16(self, synth, num_samples, left, loff, linc, right, roff, rinc):
            for i in range(num_samples):
                left[loff + i * linc] = self.left_value
                right[roff + i * rinc] = self.right_value
            return 0

        def fluid_synth_noteoff(self, synth, chan, note):
            return 0

        def delete_fluid_synth(self, synth):
            return 0

        def delete_fluid_settings(self, settings):
            return 0

    def _make_synth(self, fake_fs):
        synth = FluidSynth.__new__(FluidSynth)
        synth.fs = fake_fs
        synth.synth = object()
        synth.settings = object()
        synth.sfid = 1
        synth.sample_rate = 4
        return synth

    def test_render_sample_uses_full_velocity_and_normalizes_audio(self):
        synth = self._make_synth(self.FakeFS(left_value=1000, right_value=1000))

        rendered = synth.render_sample(bank=0, prog=0, note=60, duration_sec=1.0)

        self.assertEqual(synth.fs.noteon_velocity, 127)
        rendered_i16 = np.frombuffer(rendered, dtype=np.int16)
        self.assertTrue(np.all(rendered_i16 >= 32766))

    def test_render_sample_normalization_preserves_negative_sign(self):
        synth = self._make_synth(self.FakeFS(left_value=-1000, right_value=-1000))

        rendered = synth.render_sample(bank=0, prog=0, note=60, duration_sec=1.0)

        rendered_i16 = np.frombuffer(rendered, dtype=np.int16)
        self.assertTrue(np.all(rendered_i16 <= -32766))


if __name__ == "__main__":
    unittest.main()
