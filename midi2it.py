import os
import sys
import struct
import hashlib
import shutil
import urllib.request
from pathlib import Path

import mido
import numpy as np
import ctypes
import ctypes.util

# --- IT Format Constants ---
NUM_CHANNELS = 64
NORMALIZATION_TARGET_INT16 = 32767.0
ROW_RESOLUTION = 4
DEFAULT_SOUNDFONT_URL = (
    "https://raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/"
    "684543d5e5efaef08d02be50dcda8d552478fa60/GeneralUser-GS.sf2"
)
DEFAULT_SOUNDFONT_FILENAME = "GeneralUser-GS.sf2"
DEFAULT_SOUNDFONT_MIN_BYTES = 1_000_000

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
                    # Keep the handles alive for the process lifetime. This lets the
                    # bundled libfluidsynth DLL resolve SDL3.dll and sndfile.dll.
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

            # Prefer PyInstaller's extraction directory so Windows release EXEs use
            # the exact FluidSynth version shipped inside the executable.
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
            # Try common Homebrew paths for macOS
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
        
        # Define function signatures to prevent segfaults on 64-bit systems
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
        self.fs.delete_fluid_synth.argtypes = [ctypes.c_void_p]
        self.fs.delete_fluid_settings.argtypes = [ctypes.c_void_p]

        self.settings = self.fs.new_fluid_settings()
        self.synth = self.fs.new_fluid_synth(self.settings)
        
        # Set sample rate to 44100 for better quality in IT
        self.sample_rate = 44100
        self.fs.fluid_settings_setnum(self.settings, b"synth.sample-rate", ctypes.c_double(self.sample_rate))
        
        sf2_path_b = sf2_path.encode('utf-8')
        self.sfid = self.fs.fluid_synth_sfload(self.synth, sf2_path_b, 1)
        if self.sfid == -1:
            raise ValueError(f"Could not load SoundFont: {sf2_path}")

    def render_sample(self, bank, prog, note=60, duration_sec=1.0):
        # Render a single note to 16-bit signed PCM
        num_samples = int(self.sample_rate * duration_sec)
        
        self.fs.fluid_synth_program_select(self.synth, 0, self.sfid, bank, prog)
        self.fs.fluid_synth_noteon(self.synth, 0, note, 127)
        
        buf = (ctypes.c_short * (num_samples * 2))()
        self.fs.fluid_synth_write_s16(self.synth, num_samples, buf, 0, 2, buf, 1, 2)
        
        self.fs.fluid_synth_noteoff(self.synth, 0, note)
        
        # Convert to mono 16-bit
        data = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2).astype(np.float32)
        mono = data.mean(axis=1)
        peak = np.max(np.abs(mono))
        if peak > 0:
            mono = mono * (NORMALIZATION_TARGET_INT16 / peak)
        mono = np.clip(mono, -32768, 32767).astype(np.int16)
        
        return mono.tobytes()

    def __del__(self):
        if hasattr(self, 'fs') and hasattr(self, 'synth'):
            self.fs.delete_fluid_synth(self.synth)
        if hasattr(self, 'fs') and hasattr(self, 'settings'):
            self.fs.delete_fluid_settings(self.settings)

# --- IT Writer ---
def encode_it_text(text, length):
    return text.encode('ascii', errors='replace')[:length].ljust(length, b'\x00')


def midi_velocity_to_it_volume(velocity):
    v = max(0, min(int(velocity), 127))
    if v == 0:
        return 0
    return int(round(((v / 127.0) ** 0.5) * 64))


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


def write_it(filename, title, samples, patterns, orders, initial_tempo=125):
    # patterns: list of packed pattern bytes
    # orders: list of pattern indices
    
    num_samples = len(samples)
    num_patterns = len(patterns)
    num_orders = len(orders)
    
    # Offsets
    header_size = 192
    order_offset = header_size
    ins_offset = order_offset + num_orders
    smp_ptr_offset = ins_offset # We use 0 instruments
    pat_ptr_offset = smp_ptr_offset + (num_samples * 4)
    
    current_ptr = pat_ptr_offset + (num_patterns * 4)
    
    smp_header_ptrs = []
    for i in range(num_samples):
        smp_header_ptrs.append(current_ptr)
        current_ptr += 80 # IT Sample header size
        
    pat_header_ptrs = []
    for i in range(num_patterns):
        _, p_data = _pattern_entry(patterns[i])
        pat_header_ptrs.append(current_ptr)
        current_ptr += 8 + len(p_data)
        
    smp_data_ptrs = []
    for i in range(num_samples):
        smp_data_ptrs.append(current_ptr)
        current_ptr += len(samples[i]['data'])

    with open(filename, 'wb') as f:
        # 1. Main Header
        f.write(b"IMPM")
        f.write(encode_it_text(title, 26))
        f.write(struct.pack("<H", 0x1004)) # PHilite
        f.write(struct.pack("<H", num_orders))
        f.write(struct.pack("<H", 0)) # InsNum
        f.write(struct.pack("<H", num_samples))
        f.write(struct.pack("<H", num_patterns))
        f.write(struct.pack("<H", 0x0214)) # Cwt
        f.write(struct.pack("<H", 0x0200)) # Cmwt
        f.write(struct.pack("<H", 0x0001)) # Flags (Stereo)
        f.write(struct.pack("<H", 0x0000)) # Special
        f.write(struct.pack("B", 128)) # Global Vol
        f.write(struct.pack("B", 128)) # Mix Vol
        f.write(struct.pack("B", 6))   # Initial Speed
        tempo = int(round(initial_tempo))
        if tempo < 32:
            tempo = 32
        if tempo > 255:
            tempo = 255
        f.write(struct.pack("B", tempo)) # Initial Tempo
        f.write(struct.pack("B", 128)) # Pan Sep
        f.write(struct.pack("B", 0))   # PWD
        f.write(struct.pack("<H", 0))  # MsgLen
        f.write(struct.pack("<I", 0))  # MsgOffset
        f.write(struct.pack("<I", 0))  # Reserved
        
        # Channel Pan (64) and Vol (64)
        f.write(bytes([32] * 64)) # Center
        f.write(bytes([64] * 64)) # Max Vol
        
        # 2. Orders
        f.write(bytes(orders))
        
        # 3. Sample Pointers
        for ptr in smp_header_ptrs:
            f.write(struct.pack("<I", ptr))
            
        # 4. Pattern Pointers
        for ptr in pat_header_ptrs:
            f.write(struct.pack("<I", ptr))
            
        # 5. Sample Headers
        for i, s in enumerate(samples):
            f.seek(smp_header_ptrs[i])
            f.write(b"IMPS")
            f.write(b"sample".ljust(12, b'\x00'))
            f.write(b"\x00") # Zero
            f.write(struct.pack("B", 64)) # Global Vol
            f.write(struct.pack("B", 0x01 | 0x02)) # Flags: 1=Sample exists, 2=16-bit
            f.write(struct.pack("B", 64)) # Default Vol
            f.write(encode_it_text(s['name'], 26))
            f.write(b"\x01") # Convert (signed)
            f.write(struct.pack("B", 32)) # Default Pan
            length = len(s['data']) // 2 # 16-bit samples
            f.write(struct.pack("<I", length))
            f.write(struct.pack("<I", 0)) # Loop start
            f.write(struct.pack("<I", 0)) # Loop end
            # C5Speed: 44100 is standard for MIDI 60 if recorded at 44100
            f.write(struct.pack("<I", 44100))
            f.write(struct.pack("<I", 0)) # SusLoop start
            f.write(struct.pack("<I", 0)) # SusLoop end
            f.write(struct.pack("<I", smp_data_ptrs[i]))
            f.write(b"\x00\x00\x00\x00") # Vi, Vp, Vt, Vr

        # 6. Patterns
        for i, p_entry in enumerate(patterns):
            row_count, p_data = _pattern_entry(p_entry)
            f.seek(pat_header_ptrs[i])
            f.write(struct.pack("<H", len(p_data)))
            f.write(struct.pack("<H", row_count))
            f.write(b"\x00" * 4) # Reserved
            f.write(p_data)
            
        # 7. Sample Data
        for i, s in enumerate(samples):
            f.seek(smp_data_ptrs[i])
            f.write(s['data'])

def get_initial_bpm(mid):
    for msg in mido.merge_tracks(mid.tracks):
        if msg.type == 'set_tempo':
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


def convert_midi_to_it(midi_path, sf2_path=None, output_path=None):
    if output_path is None:
        output_path = "output.it"
    print(f"Loading MIDI: {midi_path}")
    mid = mido.MidiFile(midi_path)
    
    melodic_notes = {}
    drum_notes_used = set()
    for track in mid.tracks:
        curr_channel_programs = {i: (0, 0) for i in range(16)}
        curr_channel_programs[9] = (128, 0)
        for msg in track:
            if msg.type == "program_change":
                bank = 128 if msg.channel == 9 else 0
                curr_channel_programs[msg.channel] = (bank, msg.program)
            elif msg.type == "note_on" and msg.velocity > 0:
                if msg.channel == 9:
                    drum_notes_used.add(msg.note)
                else:
                    melodic_notes.setdefault(curr_channel_programs[msg.channel], set()).add(msg.note)

    base_note_options = [36, 60, 84, 108]
    sample_specs = []
    seen_melodic_specs = set()
    for prog in sorted(melodic_notes):
        for midi_note in sorted(melodic_notes[prog]):
            best_base = min(base_note_options, key=lambda base: abs(midi_note - base))
            key = (prog[0], prog[1], best_base)
            if key not in seen_melodic_specs:
                seen_melodic_specs.add(key)
                sample_specs.append((prog[0], prog[1], best_base, False))
    for drum_note in sorted(drum_notes_used):
        sample_specs.append((128, 0, drum_note, True))
    if not sample_specs:
        sample_specs.append((0, 0, 60, False))

    initial_bpm = get_initial_bpm(mid)
    sf2_path = resolve_soundfont(sf2_path)
    print("Loading SF2 and rendering samples...")
    fs = FluidSynth(sf2_path)
    samples = []
    sample_identity_to_id = {}
    melodic_sample_map = {}
    drum_sample_map = {}
    for bank, prog, note, is_drum in sample_specs:
        name = f"Drum {note}" if is_drum else f"Instr {prog}@{note}"
        print(f"  Recording {name}...")
        data = fs.render_sample(bank, prog, note=note)
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
            drum_sample_map[note] = sample_id
        else:
            melodic_sample_map[(bank, prog, note)] = sample_id

    if mid.ticks_per_beat <= 0:
        raise ValueError("Invalid MIDI ticks_per_beat")
    merged_track = mido.merge_tracks(mid.tracks)
    total_ticks = sum(msg.time for msg in merged_track)
    max_rows = int((total_ticks * ROW_RESOLUTION) / mid.ticks_per_beat) + 128
    row_data = [[] for _ in range(max_rows)]
    
    current_channel_programs = {i: (0, 0) for i in range(16)}
    current_channel_programs[9] = (128, 0)
    
    abs_tick = 0
    for msg in merged_track:
        abs_tick += msg.time
        if msg.type == 'program_change':
            bank = 128 if msg.channel == 9 else 0
            current_channel_programs[msg.channel] = (bank, msg.program)
        elif msg.type == 'note_on' and msg.velocity > 0:
            row_idx = int((abs_tick * ROW_RESOLUTION) / mid.ticks_per_beat)
            if row_idx >= max_rows: continue
            
            if msg.channel == 9:
                instr_idx = drum_sample_map.get(msg.note, 1)
                note_to_play = 60 # Played at original pitch
            else:
                prog = current_channel_programs[msg.channel]
                best_base = min(base_note_options, key=lambda base: abs(msg.note - base))
                instr_idx = melodic_sample_map.get((prog[0], prog[1], best_base), 1)
                # Formula: N = M - M_rec + 60
                note_to_play = msg.note - best_base + 60
            
            if note_to_play < 0: note_to_play = 0
            if note_to_play > 119: note_to_play = 119
            
            it_chan = msg.channel
            row_data[row_idx].append((it_chan, note_to_play, instr_idx, msg.velocity))

    # Pack patterns, splitting at MIDI time-signature changes.
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
            notes_in_row = row_data[row_idx] if row_idx < len(row_data) else []

            seen_channels = {}
            for it_chan, note, instr, velocity in notes_in_row:
                final_chan = it_chan
                if final_chan in seen_channels:
                    for c in range(16, NUM_CHANNELS):
                        if c not in seen_channels:
                            final_chan = c
                            break
                seen_channels[final_chan] = (note, instr, velocity)

            for it_chan, (note, instr, velocity) in sorted(seen_channels.items()):
                p_bytes.append(((it_chan & 0x3F) + 1) | 0x80)
                p_bytes.append(0x07)
                p_bytes.append(note)
                p_bytes.append(instr)
                p_bytes.append(midi_velocity_to_it_volume(velocity))
            p_bytes.append(0)
        patterns.append((row_count, bytes(p_bytes)))

    orders = list(range(len(patterns)))

    print(f"Writing IT file: {output_path}")
    write_it(output_path, os.path.basename(midi_path)[:26], samples, patterns, orders, initial_tempo=initial_bpm)
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
