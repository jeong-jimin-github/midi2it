import os
import sys
import struct
import hashlib
import shutil
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

import mido
import numpy as np
import ctypes
import ctypes.util

# --- IT Format Constants ---
NUM_CHANNELS = 64
MAX_IT_SAMPLES = 99
NORMALIZATION_TARGET_INT16 = 32767.0
ROW_RESOLUTION = 4
ROW_BOUNDARY_SNAP_DIVISOR = 8
IT_INITIAL_SPEED = 6
MIDI_DEFAULT_VOLUME = 100
MIDI_DEFAULT_EXPRESSION = 127
MIDI_DEFAULT_PAN = 64
MIDI_DEFAULT_PITCH_RANGE = 2.0
VELOCITY_LAYER_SETS = (
    (32, 64, 96, 127),
    (48, 88, 127),
    (64, 127),
    (127,),
)
DEFAULT_SOUNDFONT_URL = (
    "https://raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/"
    "684543d5e5efaef08d02be50dcda8d552478fa60/GeneralUser-GS.sf2"
)
DEFAULT_SOUNDFONT_FILENAME = "GeneralUser-GS.sf2"
DEFAULT_SOUNDFONT_MIN_BYTES = 1_000_000

# IT effect command numbers (A=1, B=2, ...)
IT_EFFECT_PORTA_DOWN = 5   # Exx
IT_EFFECT_PORTA_UP = 6     # Fxx
IT_EFFECT_CHANNEL_VOLUME = 13  # Mxx
IT_EFFECT_SET_PAN = 24     # Xxx


# --- FluidSynth Interface ---
class FluidSynth:
    _dll_directory_handles = []

    @staticmethod
    def _runtime_library_dirs():
        """Return directories that can contain FluidSynth at runtime, bundled first."""
        search_dirs = []
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            search_dirs.append(os.path.abspath(bundle_dir))
        if getattr(sys, "frozen", False):
            search_dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        search_dirs.append(os.path.dirname(os.path.abspath(__file__)))

        unique_dirs = []
        seen = set()
        for folder in search_dirs:
            normalized = os.path.normcase(os.path.abspath(folder))
            if normalized not in seen:
                seen.add(normalized)
                unique_dirs.append(folder)
        return unique_dirs

    @classmethod
    def _prepare_windows_dll_search(cls):
        if os.name != "nt" or not hasattr(os, "add_dll_directory") or cls._dll_directory_handles:
            return
        for folder in cls._runtime_library_dirs():
            if os.path.isdir(folder):
                try:
                    cls._dll_directory_handles.append(os.add_dll_directory(folder))
                except OSError:
                    continue

    @classmethod
    def _library_candidates(cls):
        candidates = []
        if os.name == "nt":
            dll_names = (
                "libfluidsynth-3.dll",
                "fluidsynth.dll",
                "libfluidsynth.dll",
                "libfluidsynth-2.dll",
            )
            for folder in cls._runtime_library_dirs():
                for dll_name in dll_names:
                    candidates.append(os.path.join(folder, dll_name))
            candidates.extend(dll_names)
            for name in ("fluidsynth", "libfluidsynth-3", "libfluidsynth-2"):
                lib = ctypes.util.find_library(name)
                if lib:
                    candidates.append(lib)
            for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(env_var)
                if not base:
                    continue
                for dll_name in dll_names:
                    candidates.append(os.path.join(base, "FluidSynth", "bin", dll_name))
        else:
            for name in ("fluidsynth", "libfluidsynth-3", "libfluidsynth-2"):
                lib = ctypes.util.find_library(name)
                if lib:
                    candidates.append(lib)
            candidates.extend([
                "/opt/homebrew/lib/libfluidsynth.dylib",
                "/usr/local/lib/libfluidsynth.dylib",
            ])
        return candidates

    @classmethod
    def _load_library(cls):
        cls._prepare_windows_dll_search()
        for lib in cls._library_candidates():
            try:
                return ctypes.CDLL(lib), lib
            except OSError:
                continue
        if os.name == "nt":
            raise ImportError(
                "FluidSynth library not found. Ensure fluidsynth.dll (or libfluidsynth-*.dll) is in PATH, next to midi2it.exe/midi2it.py, or in a standard FluidSynth install directory."
            )
        raise ImportError("FluidSynth library not found. Install it with 'brew install fluidsynth' or equivalent.")

    def __init__(self, sf2_path):
        self.fs, self.loaded_library = self._load_library()

        self.fs.new_fluid_settings.restype = ctypes.c_void_p
        self.fs.new_fluid_synth.argtypes = [ctypes.c_void_p]
        self.fs.new_fluid_synth.restype = ctypes.c_void_p
        self.fs.fluid_settings_setnum.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_double]
        self.fs.fluid_synth_sfload.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.fs.fluid_synth_sfload.restype = ctypes.c_int
        self.fs.fluid_synth_program_select.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.fs.fluid_synth_noteon.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.fs.fluid_synth_write_s16.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.fs.fluid_synth_noteoff.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        if hasattr(self.fs, "fluid_synth_system_reset"):
            self.fs.fluid_synth_system_reset.argtypes = [ctypes.c_void_p]
        self.fs.delete_fluid_synth.argtypes = [ctypes.c_void_p]
        self.fs.delete_fluid_settings.argtypes = [ctypes.c_void_p]

        self.settings = self.fs.new_fluid_settings()
        self.synth = self.fs.new_fluid_synth(self.settings)
        self.sample_rate = 44100
        self.fs.fluid_settings_setnum(self.settings, b"synth.sample-rate", ctypes.c_double(self.sample_rate))

        sf2_path_b = sf2_path.encode("utf-8")
        self.sfid = self.fs.fluid_synth_sfload(self.synth, sf2_path_b, 1)
        if self.sfid == -1:
            raise ValueError(f"Could not load SoundFont: {sf2_path}")

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
        """Render one SF2 note, including its release/effect tail.

        Conversion uses normalize=False and later applies one shared gain to the
        whole sample bank. That preserves SoundFont velocity-layer loudness and
        instrument-to-instrument balance instead of peak-normalizing every sample.
        """
        velocity = max(1, min(int(velocity), 127))
        hold_samples = max(1, int(self.sample_rate * max(0.01, duration_sec)))
        release_samples = max(0, int(self.sample_rate * max(0.0, release_sec)))

        if hasattr(self.fs, "fluid_synth_system_reset"):
            self.fs.fluid_synth_system_reset(self.synth)
        self.fs.fluid_synth_program_select(self.synth, 0, self.sfid, bank, prog)
        self.fs.fluid_synth_noteon(self.synth, 0, note, velocity)

        hold_buf = (ctypes.c_short * (hold_samples * 2))()
        self.fs.fluid_synth_write_s16(
            self.synth, hold_samples, hold_buf, 0, 2, hold_buf, 1, 2
        )
        self.fs.fluid_synth_noteoff(self.synth, 0, note)

        chunks = [np.frombuffer(hold_buf, dtype=np.int16).reshape(-1, 2).astype(np.float32)]
        if release_samples:
            release_buf = (ctypes.c_short * (release_samples * 2))()
            self.fs.fluid_synth_write_s16(
                self.synth, release_samples, release_buf, 0, 2, release_buf, 1, 2
            )
            chunks.append(
                np.frombuffer(release_buf, dtype=np.int16).reshape(-1, 2).astype(np.float32)
            )

        stereo = np.concatenate(chunks, axis=0)
        mono = stereo.mean(axis=1)
        if normalize:
            peak = np.max(np.abs(mono)) if mono.size else 0.0
            if peak > 0:
                mono = mono * (NORMALIZATION_TARGET_INT16 / peak)
        mono = np.clip(mono, -32768, 32767).astype(np.int16)
        if hasattr(self.fs, "fluid_synth_system_reset"):
            self.fs.fluid_synth_system_reset(self.synth)
        return mono.tobytes()

    def __del__(self):
        if hasattr(self, "fs") and hasattr(self, "synth"):
            self.fs.delete_fluid_synth(self.synth)
        if hasattr(self, "fs") and hasattr(self, "settings"):
            self.fs.delete_fluid_settings(self.settings)


# --- IT Writer / conversion helpers ---
def encode_it_text(text, length):
    return text.encode("ascii", errors="replace")[:length].ljust(length, b"\x00")


def midi_velocity_to_it_volume(velocity):
    """Perceptual MIDI velocity -> IT volume column (0..64)."""
    v = max(0, min(int(velocity), 127))
    if v == 0:
        return 0
    return int(round(((v / 127.0) ** 0.5) * 64))


def _velocity_relative_volume(velocity, rendered_velocity):
    """Scale a ceiling velocity layer down to the exact MIDI velocity."""
    target = midi_velocity_to_it_volume(velocity)
    reference = max(1, midi_velocity_to_it_volume(rendered_velocity))
    return max(1, min(64, int(round(64.0 * target / reference))))


def _channel_gain_to_it_volume(volume, expression):
    """Map CC7/CC11 to IT channel volume while keeping GM default CC7=100 at unity."""
    volume = max(0, min(int(volume), 127))
    expression = max(0, min(int(expression), 127))
    gain = (volume / float(MIDI_DEFAULT_VOLUME)) * (expression / 127.0)
    return max(0, min(64, int(round(64.0 * gain))))


def _midi_pan_to_it_pan(pan):
    pan = max(0, min(int(pan), 127))
    return max(0, min(64, int(round(pan * 64.0 / 127.0))))


def _midi_pan_to_effect_param(pan):
    pan = max(0, min(int(pan), 127))
    return max(0, min(255, int(round(pan * 255.0 / 127.0))))


def _default_soundfont_cache_dir():
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "midi2it" / "cache"
    cache_root = os.environ.get("XDG_CACHE_HOME")
    return (Path(cache_root) if cache_root else Path.home() / ".cache") / "midi2it"


def download_default_soundfont(cache_dir=None, url=DEFAULT_SOUNDFONT_URL):
    cache_dir = Path(cache_dir) if cache_dir is not None else _default_soundfont_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / DEFAULT_SOUNDFONT_FILENAME
    if target.exists() and target.stat().st_size >= DEFAULT_SOUNDFONT_MIN_BYTES:
        return str(target)
    temp_path = target.with_suffix(target.suffix + ".download")
    temp_path.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "midi2it/1.0"})
    print(f"No SoundFont specified. Downloading default SoundFont to: {target}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, open(temp_path, "wb") as out_file:
            shutil.copyfileobj(response, out_file, length=1024 * 1024)
        if temp_path.stat().st_size < DEFAULT_SOUNDFONT_MIN_BYTES:
            raise RuntimeError("Downloaded SoundFont is unexpectedly small or incomplete")
        os.replace(temp_path, target)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download the default SoundFont: {exc}") from exc
    return str(target)


def resolve_soundfont(sf2_path=None):
    if sf2_path:
        return str(sf2_path)
    env_soundfont = os.environ.get("MIDI2IT_SOUNDFONT")
    if env_soundfont:
        return env_soundfont
    return download_default_soundfont()


def _pattern_entry(entry):
    if isinstance(entry, tuple):
        rows, data = entry
        rows = max(1, min(int(rows), 200))
        return rows, data
    return 64, entry


def write_it(
    filename,
    title,
    samples,
    patterns,
    orders,
    initial_tempo=125,
    channel_pans=None,
    channel_volumes=None,
):
    num_samples = len(samples)
    num_patterns = len(patterns)
    num_orders = len(orders)

    header_size = 192
    order_offset = header_size
    ins_offset = order_offset + num_orders
    smp_ptr_offset = ins_offset
    pat_ptr_offset = smp_ptr_offset + (num_samples * 4)
    current_ptr = pat_ptr_offset + (num_patterns * 4)

    smp_header_ptrs = []
    for _ in range(num_samples):
        smp_header_ptrs.append(current_ptr)
        current_ptr += 80

    pat_header_ptrs = []
    for i in range(num_patterns):
        _, p_data = _pattern_entry(patterns[i])
        pat_header_ptrs.append(current_ptr)
        current_ptr += 8 + len(p_data)

    smp_data_ptrs = []
    for i in range(num_samples):
        smp_data_ptrs.append(current_ptr)
        current_ptr += len(samples[i]["data"])

    pans = list(channel_pans) if channel_pans is not None else [32] * NUM_CHANNELS
    vols = list(channel_volumes) if channel_volumes is not None else [64] * NUM_CHANNELS
    pans = (pans + [32] * NUM_CHANNELS)[:NUM_CHANNELS]
    vols = (vols + [64] * NUM_CHANNELS)[:NUM_CHANNELS]
    pans = [max(0, min(int(v), 64)) for v in pans]
    vols = [max(0, min(int(v), 64)) for v in vols]

    with open(filename, "wb") as f:
        f.write(b"IMPM")
        f.write(encode_it_text(title, 26))
        f.write(struct.pack("<H", 0x1004))
        f.write(struct.pack("<H", num_orders))
        f.write(struct.pack("<H", 0))
        f.write(struct.pack("<H", num_samples))
        f.write(struct.pack("<H", num_patterns))
        f.write(struct.pack("<H", 0x0214))
        f.write(struct.pack("<H", 0x0200))
        # Stereo + linear slides. Linear slides make MIDI pitch-bend approximation predictable.
        f.write(struct.pack("<H", 0x0001 | 0x0008))
        f.write(struct.pack("<H", 0x0000))
        f.write(struct.pack("B", 128))
        f.write(struct.pack("B", 128))
        f.write(struct.pack("B", IT_INITIAL_SPEED))
        tempo = max(32, min(255, int(round(initial_tempo))))
        f.write(struct.pack("B", tempo))
        f.write(struct.pack("B", 128))
        f.write(struct.pack("B", 0))
        f.write(struct.pack("<H", 0))
        f.write(struct.pack("<I", 0))
        f.write(struct.pack("<I", 0))

        f.write(bytes(pans))
        f.write(bytes(vols))
        f.write(bytes(orders))

        for ptr in smp_header_ptrs:
            f.write(struct.pack("<I", ptr))
        for ptr in pat_header_ptrs:
            f.write(struct.pack("<I", ptr))

        for i, s in enumerate(samples):
            f.seek(smp_header_ptrs[i])
            f.write(b"IMPS")
            f.write(b"sample".ljust(12, b"\x00"))
            f.write(b"\x00")
            f.write(struct.pack("B", 64))
            f.write(struct.pack("B", 0x01 | 0x02))
            f.write(struct.pack("B", 64))
            f.write(encode_it_text(s["name"], 26))
            f.write(b"\x01")
            f.write(struct.pack("B", 32))
            length = len(s["data"]) // 2
            f.write(struct.pack("<I", length))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 44100))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", smp_data_ptrs[i]))
            f.write(b"\x00\x00\x00\x00")

        for i, p_entry in enumerate(patterns):
            row_count, p_data = _pattern_entry(p_entry)
            f.seek(pat_header_ptrs[i])
            f.write(struct.pack("<H", len(p_data)))
            f.write(struct.pack("<H", row_count))
            f.write(b"\x00" * 4)
            f.write(p_data)

        for i, s in enumerate(samples):
            f.seek(smp_data_ptrs[i])
            f.write(s["data"])


def get_initial_bpm(mid):
    for msg in mido.merge_tracks(mid.tracks):
        if msg.type == "set_tempo":
            return int(round(mido.tempo2bpm(msg.tempo)))
    return 120


def get_time_signature_events(mid):
    events = []
    abs_tick = 0
    for msg in mido.merge_tracks(mid.tracks):
        abs_tick += msg.time
        if msg.type == "time_signature":
            numerator = int(msg.numerator) if msg.numerator > 0 else 4
            denominator = int(msg.denominator) if msg.denominator > 0 else 4
            events.append((abs_tick, numerator, denominator))
    return events


def get_initial_time_signature(mid):
    signature = (4, 4)
    for abs_tick, numerator, denominator in get_time_signature_events(mid):
        if abs_tick > 0:
            break
        signature = (numerator, denominator)
    return signature


def _rows_per_pattern_for_signature(numerator, denominator):
    rows_per_measure = max(1, int(round((numerator * 16.0) / denominator)))
    return max(1, min(rows_per_measure * 4, 200))


def _midi_tick_to_row(abs_tick, ticks_per_beat, row_resolution=ROW_RESOLUTION):
    """Map a MIDI tick to an IT row without pulling near-boundary notes early."""
    if ticks_per_beat <= 0:
        raise ValueError("Invalid MIDI ticks_per_beat")
    scaled_tick = max(0, int(abs_tick)) * int(row_resolution)
    row, remainder = divmod(scaled_tick, int(ticks_per_beat))
    if (
        remainder
        and remainder * ROW_BOUNDARY_SNAP_DIVISOR
        >= int(ticks_per_beat) * (ROW_BOUNDARY_SNAP_DIVISOR - 1)
    ):
        row += 1
    return row


def _pattern_spans_for_midi(mid, actual_last_row, row_resolution=ROW_RESOLUTION):
    if mid.ticks_per_beat <= 0:
        raise ValueError("Invalid MIDI ticks_per_beat")
    signature_by_row = {0: (4, 4)}
    for abs_tick, numerator, denominator in get_time_signature_events(mid):
        row = int((abs_tick * row_resolution) / mid.ticks_per_beat)
        if row <= actual_last_row:
            signature_by_row[row] = (numerator, denominator)
    changes = sorted(signature_by_row.items())
    spans = []
    position = 0
    change_index = 0
    signature = (4, 4)
    while position <= actual_last_row and len(spans) < 200:
        while change_index < len(changes) and changes[change_index][0] <= position:
            _, signature = changes[change_index]
            change_index += 1
        target_rows = _rows_per_pattern_for_signature(*signature)
        next_change_row = changes[change_index][0] if change_index < len(changes) else None
        row_count = target_rows
        if next_change_row is not None and position < next_change_row < position + target_rows:
            row_count = next_change_row - position
        row_count = max(1, min(row_count, 200))
        spans.append((position, row_count))
        position += row_count
    return spans


def _initial_controller_state(mid):
    states = [
        {"volume": MIDI_DEFAULT_VOLUME, "expression": MIDI_DEFAULT_EXPRESSION, "pan": MIDI_DEFAULT_PAN}
        for _ in range(16)
    ]
    abs_tick = 0
    for msg in mido.merge_tracks(mid.tracks):
        abs_tick += msg.time
        if abs_tick > 0:
            break
        if msg.type == "control_change":
            state = states[msg.channel]
            if msg.control == 7:
                state["volume"] = msg.value
            elif msg.control == 11:
                state["expression"] = msg.value
            elif msg.control == 10:
                state["pan"] = msg.value
    return states


def _program_bank(bank_msb, bank_lsb, channel):
    if channel == 9:
        return 128
    return (int(bank_msb) << 7) | int(bank_lsb)


def _collect_sample_specs(mid, base_note_options):
    merged = mido.merge_tracks(mid.tracks)
    programs = [0] * 16
    bank_msb = [0] * 16
    bank_lsb = [0] * 16
    sustain = [False] * 16
    tempo = 500000
    abs_sec = 0.0
    active = defaultdict(deque)
    pending_sustain = [[] for _ in range(16)]
    specs = set()
    longest_hold = defaultdict(lambda: 1.0)

    def finish(started, spec, ended):
        longest_hold[spec] = max(longest_hold[spec], max(0.0, ended - started))

    for msg in merged:
        abs_sec += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
            continue
        if msg.type == "control_change":
            if msg.control == 0:
                bank_msb[msg.channel] = msg.value
            elif msg.control == 32:
                bank_lsb[msg.channel] = msg.value
            elif msg.control == 64:
                was_down = sustain[msg.channel]
                sustain[msg.channel] = msg.value >= 64
                if was_down and not sustain[msg.channel]:
                    for started, spec in pending_sustain[msg.channel]:
                        finish(started, spec, abs_sec)
                    pending_sustain[msg.channel].clear()
            continue
        if msg.type == "program_change":
            programs[msg.channel] = msg.program
            continue

        is_note_on = msg.type == "note_on" and msg.velocity > 0
        is_note_off = msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)
        if is_note_on:
            bank = _program_bank(bank_msb[msg.channel], bank_lsb[msg.channel], msg.channel)
            prog = programs[msg.channel]
            if msg.channel == 9:
                base = msg.note
                spec = (bank, prog, base, True)
            else:
                base = min(base_note_options, key=lambda candidate: abs(msg.note - candidate))
                spec = (bank, prog, base, False)
            specs.add(spec)
            active[(msg.channel, msg.note)].append((abs_sec, spec))
        elif is_note_off:
            key = (msg.channel, msg.note)
            queue = active.get(key)
            if queue:
                started, spec = queue.popleft()
                if sustain[msg.channel]:
                    pending_sustain[msg.channel].append((started, spec))
                else:
                    finish(started, spec, abs_sec)
                if not queue:
                    active.pop(key, None)

    for queue in active.values():
        for started, spec in queue:
            finish(started, spec, abs_sec)
    for channel_pending in pending_sustain:
        for started, spec in channel_pending:
            finish(started, spec, abs_sec)

    if not specs:
        specs.add((0, 0, 60, False))
    return sorted(specs), longest_hold


def _choose_velocity_layers(sample_spec_count):
    count = max(1, int(sample_spec_count))
    for layers in VELOCITY_LAYER_SETS:
        if count * len(layers) <= MAX_IT_SAMPLES:
            return layers
    return (127,)


def _velocity_layer_for(velocity, layers):
    velocity = max(1, min(int(velocity), 127))
    for layer in layers:
        if velocity <= layer:
            return layer
    return layers[-1]


def _normalize_sample_bank(samples):
    peak = 0
    for sample in samples:
        data = np.frombuffer(sample["data"], dtype=np.int16)
        if data.size:
            peak = max(peak, int(np.max(np.abs(data.astype(np.int32)))))
    if peak <= 0:
        return samples
    gain = NORMALIZATION_TARGET_INT16 / float(peak)
    for sample in samples:
        data = np.frombuffer(sample["data"], dtype=np.int16).astype(np.float32)
        data = np.clip(data * gain, -32768, 32767).astype(np.int16)
        sample["data"] = data.tobytes()
    return samples


def _new_pattern_event():
    return {
        "note": None,
        "instr": None,
        "volume": None,
        "effect": None,
        "param": None,
        "effect_priority": -1,
    }


def _put_event(row_data, row, channel, note=None, instr=None, volume=None, effect=None, param=None, effect_priority=0):
    if row < 0 or row >= len(row_data) or channel < 0 or channel >= NUM_CHANNELS:
        return
    event = row_data[row].setdefault(channel, _new_pattern_event())
    if note is not None:
        event["note"] = int(note) & 0xFF
    if instr is not None:
        event["instr"] = int(instr) & 0xFF
    if volume is not None:
        event["volume"] = int(volume) & 0xFF
    if effect is not None and effect_priority >= event["effect_priority"]:
        event["effect"] = int(effect) & 0xFF
        event["param"] = int(param or 0) & 0xFF
        event["effect_priority"] = effect_priority


def _put_pan_event(row_data, row, channel, midi_pan, effect_priority=20):
    if row < 0 or row >= len(row_data):
        return
    event = row_data[row].setdefault(channel, _new_pattern_event())
    pan64 = _midi_pan_to_it_pan(midi_pan)
    if event["note"] is None and (event["volume"] is None or event["volume"] >= 128):
        event["volume"] = 128 + pan64
    else:
        _put_event(
            row_data,
            row,
            channel,
            effect=IT_EFFECT_SET_PAN,
            param=_midi_pan_to_effect_param(midi_pan),
            effect_priority=effect_priority,
        )


def _pack_pattern_event(channel, event, output):
    mask = 0
    if event["note"] is not None:
        mask |= 0x01
    if event["instr"] is not None:
        mask |= 0x02
    if event["volume"] is not None:
        mask |= 0x04
    if event["effect"] is not None:
        mask |= 0x08
    if not mask:
        return
    output.append(((channel & 0x3F) + 1) | 0x80)
    output.append(mask)
    if mask & 0x01:
        output.append(event["note"])
    if mask & 0x02:
        output.append(event["instr"])
    if mask & 0x04:
        output.append(event["volume"])
    if mask & 0x08:
        output.append(event["effect"])
        output.append(event["param"])


def _pitch_delta_effect(delta_semitones):
    if abs(delta_semitones) < 0.03:
        return None
    # IT linear Exx/Fxx uses 4*xx slide units per update, 768 units/octave.
    # With speed 6 there are about five non-row updates, so ~3.2 * semitones.
    param = max(1, min(0xDF, int(round(abs(delta_semitones) * 3.2))))
    effect = IT_EFFECT_PORTA_UP if delta_semitones > 0 else IT_EFFECT_PORTA_DOWN
    return effect, param


def convert_midi_to_it(midi_path, sf2_path=None, output_path=None):
    if output_path is None:
        output_path = "output.it"
    print(f"Loading MIDI: {midi_path}")
    mid = mido.MidiFile(midi_path)
    if mid.ticks_per_beat <= 0:
        raise ValueError("Invalid MIDI ticks_per_beat")

    base_note_options = [36, 60, 84, 108]
    sample_specs, longest_hold = _collect_sample_specs(mid, base_note_options)
    velocity_layers = _choose_velocity_layers(len(sample_specs))
    print(f"Velocity layers: {', '.join(map(str, velocity_layers))}")

    initial_bpm = get_initial_bpm(mid)
    sf2_path = resolve_soundfont(sf2_path)
    print("Loading SF2 and rendering samples...")
    fs = FluidSynth(sf2_path)
    samples = []
    sample_identity_to_id = {}
    melodic_sample_map = {}
    drum_sample_map = {}

    for bank, prog, note, is_drum in sample_specs:
        hold_sec = 1.0 if is_drum else max(1.0, min(8.0, longest_hold[(bank, prog, note, is_drum)] + 0.25))
        for layer_velocity in velocity_layers:
            name = (
                f"Drum {prog}:{note} v{layer_velocity}"
                if is_drum
                else f"Instr {bank}:{prog}@{note} v{layer_velocity}"
            )
            print(f"  Recording {name}...")
            data = fs.render_sample(
                bank,
                prog,
                note=note,
                velocity=layer_velocity,
                duration_sec=hold_sec,
                release_sec=0.75,
                normalize=False,
            )
            playback_root = 60 if is_drum else note
            identity = (playback_root, len(data), hashlib.sha256(data).digest())
            sample_id = sample_identity_to_id.get(identity)
            if sample_id is None:
                samples.append({"name": name, "data": data})
                sample_id = len(samples)
                sample_identity_to_id[identity] = sample_id
            else:
                print(f"    Reusing identical sample #{sample_id}")
            if is_drum:
                drum_sample_map[(bank, prog, note, layer_velocity)] = sample_id
            else:
                melodic_sample_map[(bank, prog, note, layer_velocity)] = sample_id

    _normalize_sample_bank(samples)

    merged_track = mido.merge_tracks(mid.tracks)
    total_ticks = sum(msg.time for msg in merged_track)
    max_rows = int((total_ticks * ROW_RESOLUTION) / mid.ticks_per_beat) + 128
    row_data = [dict() for _ in range(max_rows)]

    initial_controllers = _initial_controller_state(mid)
    channel_pans = [32] * NUM_CHANNELS
    channel_volumes = [64] * NUM_CHANNELS
    for midi_channel, state in enumerate(initial_controllers):
        pan = _midi_pan_to_it_pan(state["pan"])
        vol = _channel_gain_to_it_volume(state["volume"], state["expression"])
        for it_channel in (midi_channel, midi_channel + 16, midi_channel + 32, midi_channel + 48):
            channel_pans[it_channel] = pan
            channel_volumes[it_channel] = vol

    programs = [0] * 16
    bank_msb = [0] * 16
    bank_lsb = [0] * 16
    cc_volume = [MIDI_DEFAULT_VOLUME] * 16
    cc_expression = [MIDI_DEFAULT_EXPRESSION] * 16
    cc_pan = [MIDI_DEFAULT_PAN] * 16
    sustain = [False] * 16
    pitch_value = [0] * 16
    pitch_range = [MIDI_DEFAULT_PITCH_RANGE] * 16
    pitch_semitones = [0.0] * 16
    rpn_msb = [127] * 16
    rpn_lsb = [127] * 16
    unsupported_effect_cc = set()

    active_voice = [None] * NUM_CHANNELS
    voice_queues = defaultdict(deque)
    voice_counter = 0
    it_volume_state = list(channel_volumes)
    it_pan_state = list(channel_pans)

    def preferred_channels(midi_channel):
        return (midi_channel, midi_channel + 16, midi_channel + 32, midi_channel + 48)

    def detach_voice(it_channel):
        voice = active_voice[it_channel]
        if voice is None:
            return
        key = (voice["midi_channel"], voice["note"])
        queue = voice_queues.get(key)
        if queue:
            try:
                queue.remove(it_channel)
            except ValueError:
                pass
            if not queue:
                voice_queues.pop(key, None)
        active_voice[it_channel] = None

    def allocate_voice(midi_channel, row):
        nonlocal voice_counter
        for candidate in preferred_channels(midi_channel):
            if active_voice[candidate] is None:
                return candidate
        for candidate in range(NUM_CHANNELS):
            if active_voice[candidate] is None:
                return candidate
        # All 64 channels are busy: replace the oldest voice. A new note naturally
        # cuts the old sample on that IT channel, so an extra note-cut is unnecessary.
        candidate = min(
            range(NUM_CHANNELS),
            key=lambda c: active_voice[c]["age"] if active_voice[c] is not None else -1,
         )
        detach_voice(candidate)
        return candidate

    def target_channels_for_controller(midi_channel):
        channels = set(preferred_channels(midi_channel))
        channels.update(
            idx
            for idx, voice in enumerate(active_voice)
            if voice is not None and voice["midi_channel"] == midi_channel
        )
        return sorted(channels)

    def release_voice(midi_channel, note, row, force=False):
        queue = voice_queues.get((midi_channel, note))
        if not queue:
            return
        it_channel = queue[0]
        voice = active_voice[it_channel]
        if voice is None:
            queue.popleft()
            return
        if sustain[midi_channel] and not force:
            voice["pending_release"] = True
            return
        _put_event(row_data, row, it_channel, note=254)
        detach_voice(it_channel)

    abs_tick = 0
    for msg in merged_track:
        abs_tick += msg.time
        row_idx = _midi_tick_to_row(abs_tick, mid.ticks_per_beat)
        if row_idx >= max_rows:
            continue

        if msg.type == "program_change":
            programs[msg.channel] = msg.program
            continue

        if msg.type == "control_change":
            ch = msg.channel
            ctl = msg.control
            val = msg.value
            if ctl == 0:
                bank_msb[ch] = val
            elif ctl == 32:
                bank_lsb[ch] = val
            elif ctl == 7 or ctl == 11:
                if ctl == 7:
                    cc_volume[ch] = val
                else:
                    cc_expression[ch] = val
                target_volume = _channel_gain_to_it_volume(cc_volume[ch], cc_expression[ch])
                for it_channel in target_channels_for_controller(ch):
                    if it_volume_state[it_channel] != target_volume:
                        _put_event(
                            row_data,
                            row_idx,
                            it_channel,
                            effect=IT_EFFECT_CHANNEL_VOLUME,
                            param=target_volume,
                            effect_priority=30,
                        )
                        it_volume_state[it_channel] = target_volume
            elif ctl == 10:
                cc_pan[ch] = val
                target_pan = _midi_pan_to_it_pan(val)
                for it_channel in target_channels_for_controller(ch):
                    if it_pan_state[it_channel] != target_pan:
                        _put_pan_event(row_data, row_idx, it_channel, val, effect_priority=25)
                        it_pan_state[it_channel] = target_pan
            elif ctl == 64:
                was_sustain = sustain[ch]
                sustain[ch] = val >= 64
                if was_sustain and not sustain[ch]:
                    for it_channel, voice in list(enumerate(active_voice)):
                        if voice is not None and voice["midi_channel"] == ch and voice.get("pending_release"):
                            _put_event(row_data, row_idx, it_channel, note=254)
                            detach_voice(it_channel)
            elif ctl in (120, 123):
                for it_channel, voice in list(enumerate(active_voice)):
                    if voice is not None and voice["midi_channel"] == ch:
                        _put_event(row_data, row_idx, it_channel, note=254)
                        detach_voice(it_channel)
            elif ctl == 121:
                cc_volume[ch] = MIDI_DEFAULT_VOLUME
                cc_expression[ch] = MIDI_DEFAULT_EXPRESSION
                cc_pan[ch] = MIDI_DEFAULT_PAN
                sustain[ch] = False
                pitch_value[ch] = 0
                pitch_semitones[ch] = 0.0
            elif ctl == 101:
                rpn_msb[ch] = val
            elif ctl == 100:
                rpn_lsb[ch] = val
            elif ctl == 6 and rpn_msb[ch] == 0 and rpn_lsb[ch] == 0:
                pitch_range[ch] = max(0.0, min(float(val), 24.0))
            elif ctl in (1, 91, 93):
                # IT sample mode has no direct General MIDI modulation/reverb/chorus send.
                # The SoundFont's default reverb/chorus tail is baked into rendered PCM,
                # but dynamic send/mod-wheel changes cannot be represented exactly.
                unsupported_effect_cc.add(ctl)
            continue

        if msg.type == "pitchwheel":
            ch = msg.channel
            old_semitones = pitch_semitones[ch]
            pitch_value[ch] = msg.pitch
            new_semitones = (msg.pitch / 8192.0) * pitch_range[ch]
            pitch_semitones[ch] = new_semitones
            effect = _pitch_delta_effect(new_semitones - old_semitones)
            if effect is not None:
                effect_code, param = effect
                for it_channel, voice in enumerate(active_voice):
                    if voice is not None and voice["midi_channel"] == ch:
                        _put_event(
                            row_data,
                            row_idx,
                            it_channel,
                            effect=effect_code,
                            param=param,
                            effect_priority=50,
                        )
            continue

        is_note_on = msg.type == "note_on" and msg.velocity > 0
        is_note_off = msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)
        if is_note_off:
            release_voice(msg.channel, msg.note, row_idx)
            continue
        if not is_note_on:
            continue

        ch = msg.channel
        bank = _program_bank(bank_msb[ch], bank_lsb[ch], ch)
        prog = programs[ch]
        layer_velocity = _velocity_layer_for(msg.velocity, velocity_layers)
        if ch == 9:
            instr_idx = drum_sample_map.get((bank, prog, msg.note, layer_velocity), 1)
            note_to_play = 60
        else:
            best_base = min(base_note_options, key=lambda base: abs(msg.note - base))
            instr_idx = melodic_sample_map.get((bank, prog, best_base, layer_velocity), 1)
            note_to_play = msg.note - best_base + 60 + int(round(pitch_semitones[ch]))
        note_to_play = max(0, min(119, note_to_play))

        it_channel = allocate_voice(ch, row_idx)
        voice_counter += 1
        active_voice[it_channel] = {
            "midi_channel": ch,
            "note": msg.note,
            "age": voice_counter,
            "pending_release": False,
        }
        voice_queues[(ch, msg.note)].append(it_channel)

        note_volume = _velocity_relative_volume(msg.velocity, layer_velocity)
        _put_event(
            row_data,
            row_idx,
            it_channel,
            note=note_to_play,
            instr=instr_idx,
            volume=note_volume,
        )

        desired_volume = _channel_gain_to_it_volume(cc_volume[ch], cc_expression[ch])
        desired_pan = _midi_pan_to_it_pan(cc_pan[ch])
        if it_volume_state[it_channel] != desired_volume:
            _put_event(
                row_data,
                row_idx,
                it_channel,
                effect=IT_EFFECT_CHANNEL_VOLUME,
                param=desired_volume,
                effect_priority=40,
            )
            it_volume_state[it_channel] = desired_volume
        elif it_pan_state[it_channel] != desired_pan:
            _put_event(
                row_data,
                row_idx,
                it_channel,
                effect=IT_EFFECT_SET_PAN,
                param=_midi_pan_to_effect_param(cc_pan[ch]),
                effect_priority=35,
            )
            it_pan_state[it_channel] = desired_pan

    if unsupported_effect_cc:
        labels = {1: "CC1 modulation", 91: "CC91 reverb send", 93: "CC93 chorus send"}
        names = ", ".join(labels[cc] for cc in sorted(unsupported_effect_cc))
        print(
            "Warning: " + names +
            " cannot be represented exactly by standard IT sample-mode effects; "
            "SoundFont default effect tails are baked into the rendered samples."
        )

    actual_last_row = 0
    for r_idx, rd in enumerate(row_data):
        if rd:
            actual_last_row = r_idx

    spans = _pattern_spans_for_midi(mid, actual_last_row)
    patterns = []
    print(f"Processing {len(spans)} patterns...")
    for start_row, row_count in spans:
        p_bytes = bytearray()
        for r in range(row_count):
            row_idx = start_row + r
            events = row_data[row_idx] if row_idx < len(row_data) else {}
            for it_channel, event in sorted(events.items()):
                _pack_pattern_event(it_channel, event, p_bytes)
            p_bytes.append(0)
        patterns.append((row_count, bytes(p_bytes)))

    orders = list(range(len(patterns)))
    print(f"Writing IT file: {output_path}")
    write_it(
        output_path,
        os.path.basename(midi_path)[:26],
        samples,
        patterns,
        orders,
        initial_tempo=initial_bpm,
        channel_pans=channel_pans,
        channel_volumes=channel_volumes,
    )
    print("Done!")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Convert MIDI files to Impulse Tracker (.it) modules")
    parser.add_argument("midi", nargs="?", help="Input MIDI file")
    parser.add_argument("legacy_soundfont", nargs="?", help="Optional SF2 path; omitted uses an auto-downloaded default")
    parser.add_argument("legacy_output", nargs="?", help="Optional output IT path")
    parser.add_argument("-s", "--soundfont", help="Optional SF2 SoundFont path")
    parser.add_argument("-o", "--output", help="Output IT path (default: output.it)")
    parser.add_argument("--check-fluidsynth", action="store_true", help="Check that the FluidSynth library can be loaded and exit")
    args = parser.parse_args(argv)

    if args.check_fluidsynth:
        try:
            _, loaded_library = FluidSynth._load_library()
            print(f"FluidSynth library loaded: {loaded_library}")
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.midi:
        parser.error("the following arguments are required: midi")

    soundfont = args.soundfont or args.legacy_soundfont
    output = args.output or args.legacy_output
    if args.soundfont is None and args.output is None and args.legacy_output is None and soundfont and str(soundfont).lower().endswith(".it"):
        output = soundfont
        soundfont = None
    if output is None:
        output = "output.it"

    try:
        convert_midi_to_it(args.midi, soundfont, output)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
