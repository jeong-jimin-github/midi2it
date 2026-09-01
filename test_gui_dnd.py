import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from midi2it_gui import COPY, REFUSE_DROP, Midi2ItApp, _drop_path_kind, _normalize_dropped_path


class DummyVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class GuiDropTests(unittest.TestCase):
    def _app_stub(self, dropped_paths):
        app = Midi2ItApp.__new__(Midi2ItApp)
        app.midi_var = DummyVar()
        app.soundfont_var = DummyVar()
        app.output_var = DummyVar()
        app.status_var = DummyVar()
        app._dropped_paths = lambda event: tuple(dropped_paths)
        return app

    def test_drop_path_kind(self):
        self.assertEqual(_drop_path_kind("song.mid"), "midi")
        self.assertEqual(_drop_path_kind("song.MIDI"), "midi")
        self.assertEqual(_drop_path_kind("bank.SF2"), "soundfont")
        self.assertIsNone(_drop_path_kind("notes.txt"))

    def test_file_uri_is_normalized(self):
        normalized = _normalize_dropped_path("file:///tmp/My%20Song.mid")
        self.assertTrue(normalized.endswith(os.path.join("tmp", "My Song.mid")))

    def test_midi_drop_returns_copy_and_populates_output(self):
        with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as tmp:
            path = tmp.name
        try:
            app = self._app_stub([path])
            action = app._drop_midi(SimpleNamespace())
            self.assertEqual(action, COPY)
            self.assertEqual(app.midi_var.get(), path)
            self.assertEqual(app.output_var.get(), os.path.splitext(path)[0] + ".it")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_wrong_file_drop_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            path = tmp.name
        try:
            app = self._app_stub([path])
            self.assertEqual(app._drop_midi(SimpleNamespace()), REFUSE_DROP)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_window_drop_auto_detects_midi_and_soundfont(self):
        with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as midi_tmp, tempfile.NamedTemporaryFile(suffix=".sf2", delete=False) as sf2_tmp:
            midi_path = midi_tmp.name
            sf2_path = sf2_tmp.name
        try:
            app = self._app_stub([midi_path, sf2_path])
            action = app._drop_any(SimpleNamespace())
            self.assertEqual(action, COPY)
            self.assertEqual(app.midi_var.get(), midi_path)
            self.assertEqual(app.soundfont_var.get(), sf2_path)
        finally:
            Path(midi_path).unlink(missing_ok=True)
            Path(sf2_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
