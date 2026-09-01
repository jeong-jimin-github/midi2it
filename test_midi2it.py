import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mido
import numpy as np

from midi2it import (
    FluidSynth,
    IT_EFFECT_CHANNEL_VOLUME,
    IT_EFFECT_PORTA_DOWN,
    IT_EFFECT_PORTA_UP,
    _channel_gain_to_it_volume,
    _choose_velocity_layers,
    _midi_pan_to_it_pan,
    _midi_tick_to_row,
    _normalize_sample_bank,
    _pattern_spans_for_midi,
    _pitch_delta_effect,
    _velocity_layer_for,
    _velocity_relative_volume,
    convert_midi_to_it,
    download_default_soundfont,
    encode_it_text,
    get_initial_time_signature,
    midi_velocity_to_it_volume,
    write_it,
)
from midi2it_gui import _output_dialog_defaults

NEAR_MAX_INT16 = 32766
NEAR_MIN_INT16 = -32766


def decode_patterns(patterns):
    rows = []
    absolute_row = 0
    for row_count, data in patterns:
        idx = 0
        for _ in range(row_count):
            events = []
            while True:
                channel_byte = data[idx]
                idx += 1
                if channel_byte == 0:
                    break
                channel = (channel_byte - 1) & 63
                mask = data[idx]
                idx += 1
                event = {"channel": channel, "mask": mask}
                if mask & 0x01:
                    event["note"] = data[idx]
                    idx += 1
                if mask & 0x02:
                    event["instr"] = data[idx]
                    idx += 1
                if mask & 0x04:
                    event["volume"] = data[idx]
                    idx += 1
                if mask & 0x08:
                    event["effect"] = data[idx]
                    event["param"] = data[idx + 1]
                    idx += 2
                events.append(event)
            if events:
                rows.append((absolute_row, events))
            absolute_row += 1
    return rows


def note_rows(patterns):
    return [
        row
        for row, events in decode_patterns(patterns)
        if any("note" in event and event["note"] < 120 for event in events)
    ]


class FakeFluidSynth:
    calls = []
    payload = b"\x10\x00" * 16

    def __init__(self, sf2_path):
        self.sf2_path = sf2_path

    def render_sample(
        self,
        bank,
        prog,
        note=60,
        velocity=127,
        duration_sec=1.0,
        release_sec=0.75,
        normalize=True,
    ):
        type(self).calls.append(
            {
                "bank": bank,
                "prog": prog,
                "note": note,
                "velocity": velocity,
                "duration_sec": duration_sec,
                "release_sec": release_sec,
                "normalize": normalize,
            }
        )
        return type(self).payload


class BasicHelpersTests(unittest.TestCase):
    def test_encode_it_text_replaces_non_latin(self):
        self.assertEqual(encode_it_text("한글abc", 6), b"??abc\x00")

    def test_encode_it_text_truncates(self):
        self.assertEqual(encode_it_text("abcdefgh", 5), b"abcde")

    def test_encode_it_text_pads(self):
        self.assertEqual(encode_it_text("abc", 5), b"abc\x00\x00")

    def test_velocity_mapping_clamps(self):
        self.assertEqual(midi_velocity_to_it_volume(-1), 0)
        self.assertEqual(midi_velocity_to_it_volume(0), 0)
        self.assertEqual(midi_velocity_to_it_volume(127), 64)
        self.assertEqual(midi_velocity_to_it_volume(200), 64)

    def test_velocity_mapping_mid_values(self):
        self.assertEqual(midi_velocity_to_it_volume(64), 45)
        self.assertEqual(midi_velocity_to_it_volume(100), 57)

    def test_velocity_layer_ceiling(self):
        layers = (32, 64, 96, 127)
        self.assertEqual(_velocity_layer_for(1, layers), 32)
        self.assertEqual(_velocity_layer_for(64, layers), 64)
        self.assertEqual(_velocity_layer_for(65, layers), 96)
        self.assertEqual(_velocity_layer_for(127, layers), 127)

    def test_velocity_relative_volume_is_full_at_layer(self):
        self.assertEqual(_velocity_relative_volume(64, 64), 64)
        self.assertLess(_velocity_relative_volume(50, 64), 64)

    def test_velocity_layers_adapt_to_sample_limit(self):
        self.assertEqual(_choose_velocity_layers(10), (32, 64, 96, 127))
        self.assertEqual(_choose_velocity_layers(30), (48, 88, 127))
        self.assertEqual(_choose_velocity_layers(60), (127,))

    def test_channel_gain_keeps_gm_default_at_full_it_volume(self):
        self.assertEqual(_channel_gain_to_it_volume(100, 127), 64)
        self.assertEqual(_channel_gain_to_it_volume(100, 0), 0)
        self.assertLess(_channel_gain_to_it_volume(50, 127), 64)

    def test_pan_mapping(self):
        self.assertEqual(_midi_pan_to_it_pan(0), 0)
        self.assertIn(_midi_pan_to_it_pan(64), (32, 33))
        self.assertEqual(_midi_pan_to_it_pan(127), 64)

    def test_pitch_delta_mapping(self):
        self.assertIsNone(_pitch_delta_effect(0.0))
        self.assertEqual(_pitch_delta_effect(1.0)[0], IT_EFFECT_PORTA_UP)
        self.assertEqual(_pitch_delta_effect(-1.0)[0], IT_EFFECT_PORTA_DOWN)


class GuiPathTests(unittest.TestCase):
    def test_output_dialog_keeps_existing_filename(self):
        initial_dir, initial_file = _output_dialog_defaults(
            os.path.join("old", "folder", "song.it"),
            os.path.join("midi", "input.mid"),
        )
        self.assertEqual(initial_file, "song.it")
        self.assertTrue(initial_dir.endswith(os.path.join("old", "folder")))

    def test_output_dialog_uses_midi_filename_when_output_empty(self):
        initial_dir, initial_file = _output_dialog_defaults(
            "", os.path.join("midi", "my song.mid")
        )
        self.assertEqual(initial_file, "my song.it")
        self.assertTrue(initial_dir.endswith("midi"))


class ItWriterTests(unittest.TestCase):
    def _write_and_read_header(self, **kwargs):
        with tempfile.NamedTemporaryFile(suffix=".it", delete=False) as tmp:
            path = tmp.name
        try:
            write_it(
                path,
                "test",
                samples=[{"name": "sample", "data": b"\x00\x00"}],
                patterns=[b"\x00"],
                orders=[0],
                **kwargs,
            )
            return Path(path).read_bytes()[:192]
        finally:
            Path(path).unlink(missing_ok=True)

    def test_max_mix_volume(self):
        data = self._write_and_read_header()
        self.assertEqual(data[49], 128)

    def test_initial_tempo(self):
        data = self._write_and_read_header(initial_tempo=90)
        self.assertEqual(data[51], 90)

    def test_linear_slide_flag_is_enabled(self):
        data = self._write_and_read_header()
        flags = int.from_bytes(data[44:46], "little")
        self.assertTrue(flags & 0x0008)

    def test_channel_pan_and_volume_headers(self):
        pans = [32] * 64
        vols = [64] * 64
        pans[0] = 7
        vols[0] = 41
        data = self._write_and_read_header(channel_pans=pans, channel_volumes=vols)
        self.assertEqual(data[64], 7)
        self.assertEqual(data[128], 41)


class TimingTests(unittest.TestCase):
    def test_notes_just_before_boundary_snap_forward(self):
        self.assertEqual(_midi_tick_to_row(479, 480), 4)
        self.assertEqual(_midi_tick_to_row(1918, 480), 16)
        self.assertEqual(_midi_tick_to_row(1919, 480), 16)

    def test_genuine_off_grid_note_stays(self):
        self.assertEqual(_midi_tick_to_row(705, 120), 23)

    def test_late_time_signature_does_not_change_song_start(self):
        mid = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.Message("note_on", note=60, velocity=100, time=0, channel=0))
        track.append(mido.MetaMessage("time_signature", numerator=3, denominator=4, time=480))
        self.assertEqual(get_initial_time_signature(mid), (4, 4))

    def test_time_signature_change_splits_pattern(self):
        mid = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
        track.append(mido.MetaMessage("time_signature", numerator=3, denominator=4, time=1920))
        spans = _pattern_spans_for_midi(mid, 80)
        self.assertEqual(spans[0], (0, 16))
        self.assertEqual(spans[1][0], 16)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        FakeFluidSynth.calls = []
        FakeFluidSynth.payload = b"\x10\x00" * 16

    def _convert(self, messages, ticks_per_beat=480):
        tmpdir = tempfile.TemporaryDirectory()
        midi_path = Path(tmpdir.name) / "input.mid"
        out_path = Path(tmpdir.name) / "output.it"
        mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        for msg in messages:
            track.append(msg)
        mid.save(midi_path)
        capture = {}

        def fake_write(*args, **kwargs):
            capture["samples"] = args[2]
            capture["patterns"] = args[3]
            capture["orders"] = args[4]
            capture["kwargs"] = kwargs

        with patch("midi2it.FluidSynth", FakeFluidSynth), patch("midi2it.write_it", fake_write):
            convert_midi_to_it(str(midi_path), "dummy.sf2", str(out_path))
        capture["rows"] = decode_patterns(capture["patterns"])
        capture["tmpdir"] = tmpdir
        return capture

    def test_passes_initial_bpm_to_writer(self):
        result = self._convert([
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90), time=0),
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertEqual(result["kwargs"]["initial_tempo"], 90)

    def test_time_signature_sets_pattern_length(self):
        result = self._convert([
            mido.MetaMessage("time_signature", numerator=3, denominator=4, time=0),
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("note_on", note=64, velocity=100, time=480, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertEqual(result["patterns"][0][0], 48)

    def test_non_divisible_ticks_per_beat_keep_alignment(self):
        messages = [mido.Message("note_on", note=60, velocity=100, time=0, channel=0)]
        messages.extend(
            mido.Message("note_on", note=60, velocity=100, time=101, channel=0)
            for _ in range(11)
        )
        result = self._convert(messages, ticks_per_beat=101)
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertEqual(note_rows(result["patterns"])[:12], [i * 4 for i in range(12)])

    def test_overlapping_notes_use_different_it_channels(self):
        result = self._convert([
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("note_on", note=64, velocity=100, time=120, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        notes = [
            event
            for _, events in result["rows"]
            for event in events
            if "note" in event and event["note"] < 120
        ]
        self.assertGreaterEqual(len(notes), 2)
        self.assertNotEqual(notes[0]["channel"], notes[1]["channel"])

    def test_note_off_becomes_note_cut(self):
        result = self._convert([
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("note_off", note=60, velocity=0, time=480, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertTrue(any(event.get("note") == 254 for _, events in result["rows"] for event in events))

    def test_sustain_defers_note_cut_until_pedal_release(self):
        result = self._convert([
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("control_change", control=64, value=127, time=120, channel=0),
            mido.Message("note_off", note=60, velocity=0, time=120, channel=0),
            mido.Message("control_change", control=64, value=0, time=240, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        cuts = [row for row, events in result["rows"] if any(event.get("note") == 254 for event in events)]
        self.assertEqual(cuts, [4])

    def test_expression_change_emits_channel_volume_effect(self):
        result = self._convert([
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("control_change", control=11, value=64, time=480, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertTrue(any(
            event.get("effect") == IT_EFFECT_CHANNEL_VOLUME
            for _, events in result["rows"]
            for event in events
        ))

    def test_initial_pan_is_written_to_it_channel_header(self):
        result = self._convert([
            mido.Message("control_change", control=10, value=20, time=0, channel=0),
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertEqual(result["kwargs"]["channel_pans"][0], _midi_pan_to_it_pan(20))

    def test_bank_select_reaches_fluidsynth_render(self):
        result = self._convert([
            mido.Message("control_change", control=0, value=2, time=0, channel=0),
            mido.Message("control_change", control=32, value=3, time=0, channel=0),
            mido.Message("program_change", program=5, time=0, channel=0),
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertTrue(FakeFluidSynth.calls)
        self.assertEqual(FakeFluidSynth.calls[0]["bank"], (2 << 7) | 3)
        self.assertEqual(FakeFluidSynth.calls[0]["prog"], 5)

    def test_velocity_layers_are_rendered_at_real_velocities(self):
        result = self._convert([
            mido.Message("note_on", note=60, velocity=20, time=0, channel=0),
            mido.Message("note_off", note=60, velocity=0, time=120, channel=0),
            mido.Message("note_on", note=60, velocity=120, time=120, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        rendered_velocities = {call["velocity"] for call in FakeFluidSynth.calls}
        self.assertEqual(rendered_velocities, {32, 64, 96, 127})
        self.assertTrue(all(call["normalize"] is False for call in FakeFluidSynth.calls))

    def test_pitch_bend_emits_pitch_slide(self):
        result = self._convert([
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("pitchwheel", pitch=4096, time=480, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertTrue(any(
            event.get("effect") in (IT_EFFECT_PORTA_UP, IT_EFFECT_PORTA_DOWN)
            for _, events in result["rows"]
            for event in events
        ))

    def test_identical_rendered_samples_are_deduplicated(self):
        result = self._convert([
            mido.Message("program_change", program=1, time=0, channel=0),
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
            mido.Message("program_change", program=2, time=480, channel=0),
            mido.Message("note_on", note=60, velocity=100, time=0, channel=0),
        ])
        self.addCleanup(result["tmpdir"].cleanup)
        self.assertEqual(len(result["samples"]), 1)


class SampleNormalizationTests(unittest.TestCase):
    def test_shared_normalization_preserves_relative_amplitude(self):
        samples = [
            {"name": "quiet", "data": np.array([1000, -1000], dtype=np.int16).tobytes()},
            {"name": "loud", "data": np.array([2000, -2000], dtype=np.int16).tobytes()},
        ]
        _normalize_sample_bank(samples)
        quiet = np.frombuffer(samples[0]["data"], dtype=np.int16).astype(np.int32)
        loud = np.frombuffer(samples[1]["data"], dtype=np.int16).astype(np.int32)
        self.assertAlmostEqual(abs(loud[0]) / abs(quiet[0]), 2.0, places=2)
        self.assertGreaterEqual(abs(loud[0]), NEAR_MAX_INT16)


class SoundFontDownloadTests(unittest.TestCase):
    def test_default_soundfont_download_is_cached(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            payload = b"RIFFfake-sf2-data"
            with patch("midi2it.DEFAULT_SOUNDFONT_MIN_BYTES", 4), patch(
                "midi2it.urllib.request.urlopen", return_value=io.BytesIO(payload)
            ) as urlopen:
                first = download_default_soundfont(cache_dir=cache_dir, url="https://example.invalid/test.sf2")
                second = download_default_soundfont(cache_dir=cache_dir, url="https://example.invalid/test.sf2")
            self.assertEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), payload)
            self.assertEqual(urlopen.call_count, 1)


class FluidSynthDiscoveryTests(unittest.TestCase):
    def test_runtime_library_dirs_prefer_pyinstaller_bundle(self):
        with tempfile.TemporaryDirectory() as bundle_dir, patch.object(sys, "_MEIPASS", bundle_dir, create=True):
            directories = FluidSynth._runtime_library_dirs()
        self.assertEqual(directories[0], os.path.abspath(bundle_dir))

    @unittest.skipUnless(os.name == "nt", "Windows-only DLL candidate test")
    def test_windows_candidates_include_bundled_fluidsynth_first(self):
        with tempfile.TemporaryDirectory() as bundle_dir, patch.object(sys, "_MEIPASS", bundle_dir, create=True):
            candidates = FluidSynth._library_candidates()
        self.assertEqual(candidates[0], os.path.join(os.path.abspath(bundle_dir), "libfluidsynth-3.dll"))

    def test_load_library_returns_loaded_candidate(self):
        fake_library = object()
        with patch.object(FluidSynth, "_prepare_windows_dll_search"), patch.object(
            FluidSynth, "_library_candidates", return_value=["bundled.dll"]
        ), patch("midi2it.ctypes.CDLL", return_value=fake_library):
            library, path = FluidSynth._load_library()
        self.assertIs(library, fake_library)
        self.assertEqual(path, "bundled.dll")


class FluidSynthRenderTests(unittest.TestCase):
    class FakeFS:
        def __init__(self, left_value=1000, right_value=1000):
            self.left_value = left_value
            self.right_value = right_value
            self.noteon_velocity = None
            self.reset_count = 0

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

        def fluid_synth_system_reset(self, synth):
            self.reset_count += 1
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

    def test_render_sample_uses_requested_velocity(self):
        synth = self._make_synth(self.FakeFS())
        synth.render_sample(bank=0, prog=0, note=60, velocity=72, duration_sec=1.0)
        self.assertEqual(synth.fs.noteon_velocity, 72)

    def test_render_sample_normalizes_positive_audio(self):
        synth = self._make_synth(self.FakeFS(left_value=1000, right_value=1000))
        rendered = synth.render_sample(bank=0, prog=0, note=60, duration_sec=1.0)
        rendered_i16 = np.frombuffer(rendered, dtype=np.int16)
        self.assertTrue(np.all(rendered_i16 >= NEAR_MAX_INT16))

    def test_render_sample_normalization_preserves_negative_sign(self):
        synth = self._make_synth(self.FakeFS(left_value=-1000, right_value=-1000))
        rendered = synth.render_sample(bank=0, prog=0, note=60, duration_sec=1.0)
        rendered_i16 = np.frombuffer(rendered, dtype=np.int16)
        self.assertTrue(np.all(rendered_i16 <= NEAR_MIN_INT16))

    def test_render_sample_can_return_unscaled_pcm(self):
        synth = self._make_synth(self.FakeFS(left_value=1000, right_value=1000))
        rendered = synth.render_sample(
            bank=0, prog=0, note=60, velocity=80, duration_sec=1.0, release_sec=0.0, normalize=False
        )
        self.assertTrue(np.all(np.frombuffer(rendered, dtype=np.int16) == 1000))


if __name__ == "__main__":
    unittest.main()
