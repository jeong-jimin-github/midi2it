import os
import sys
import struct
import mido
import numpy as np
import ctypes
import ctypes.util

# --- IT Format Constants ---
NUM_CHANNELS = 64

# --- FluidSynth Interface ---
class FluidSynth:
    @staticmethod
    def _library_candidates():
        candidates = []
        for name in ("fluidsynth", "libfluidsynth-3", "libfluidsynth-2"):
            lib = ctypes.util.find_library(name)
            if lib:
                candidates.append(lib)

        if os.name == "nt":
            dll_names = (
                "fluidsynth.dll",
                "libfluidsynth.dll",
                "libfluidsynth-3.dll",
                "libfluidsynth-2.dll",
            )
            candidates.extend(dll_names)

            search_dirs = [os.path.dirname(os.path.abspath(__file__))]
            if getattr(sys, "frozen", False):
                search_dirs.insert(0, os.path.dirname(sys.executable))

            for folder in search_dirs:
                for dll_name in dll_names:
                    candidates.append(os.path.join(folder, dll_name))

            for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(env_var)
                if not base:
                    continue
                for dll_name in dll_names:
                    candidates.append(os.path.join(base, "FluidSynth", "bin", dll_name))
        else:
            # Try common Homebrew paths for macOS
            candidates.extend([
                "/opt/homebrew/lib/libfluidsynth.dylib",
                "/usr/local/lib/libfluidsynth.dylib",
            ])

        return candidates

    def __init__(self, sf2_path):
        self.fs = None
        for lib in self._library_candidates():
            try:
                self.fs = ctypes.CDLL(lib)
                break
            except OSError:
                continue

        if not self.fs:
            if os.name == "nt":
                raise ImportError(
                    "FluidSynth library not found. Ensure fluidsynth.dll (or libfluidsynth-*.dll) is in PATH, next to midi2it.exe/midi2it.py, or in a standard FluidSynth install directory."
                )
            raise ImportError("FluidSynth library not found. Install it with 'brew install fluidsynth' or equivalent.")
        
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
        self.fs.fluid_synth_noteon(self.synth, 0, note, 100)
        
        buf = (ctypes.c_short * (num_samples * 2))()
        self.fs.fluid_synth_write_s16(self.synth, num_samples, buf, 0, 2, buf, 1, 2)
        
        self.fs.fluid_synth_noteoff(self.synth, 0, note)
        
        # Convert to mono 16-bit
        data = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2)
        mono = data.mean(axis=1).astype(np.int16)
        
        return mono.tobytes()

    def __del__(self):
        if hasattr(self, 'fs') and hasattr(self, 'synth'):
            self.fs.delete_fluid_synth(self.synth)
        if hasattr(self, 'fs') and hasattr(self, 'settings'):
            self.fs.delete_fluid_settings(self.settings)

# --- IT Writer ---
def encode_it_text(text, length):
    return text.encode('ascii', errors='replace')[:length].ljust(length, b'\x00')


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
        pat_header_ptrs.append(current_ptr)
        current_ptr += 8 + len(patterns[i])
        
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
        f.write(struct.pack("B", 48))  # Mix Vol
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
        for i, p_data in enumerate(patterns):
            f.seek(pat_header_ptrs[i])
            f.write(struct.pack("<H", len(p_data)))
            f.write(struct.pack("<H", 64)) # Rows
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


def convert_midi_to_it(midi_path, sf2_path, output_path):
    print(f"Loading MIDI: {midi_path}")
    mid = mido.MidiFile(midi_path)
    
    # Track which (bank, program) is used on which channel
    channel_programs = {i: (0, 0) for i in range(16)}
    channel_programs[9] = (128, 0) # MIDI channel 10 is index 9
    
    melodic_notes = {} # (bank, program) -> set of notes
    drum_notes_used = set()
    
    # First pass: find all instruments and notes used
    for track in mid.tracks:
        curr_channel_programs = {i: (0, 0) for i in range(16)}
        curr_channel_programs[9] = (128, 0)
        for msg in track:
            if msg.type == 'program_change':
                bank = 128 if msg.channel == 9 else 0
                curr_channel_programs[msg.channel] = (bank, msg.program)
            elif msg.type == 'note_on' and msg.velocity > 0:
                if msg.channel == 9:
                    drum_notes_used.add(msg.note)
                else:
                    prog = curr_channel_programs[msg.channel]
                    if prog not in melodic_notes:
                        melodic_notes[prog] = set()
                    melodic_notes[prog].add(msg.note)

    # Assign IDs: multisampled melodic first, then drums
    all_instruments = [] # list of (bank, prog, note, is_drum)
    melodic_sample_map = {} # (bank, prog, base_note) -> sample_id (1-based)
    drum_sample_map = {} # note -> sample_id (1-based)
    
    base_note_options = [36, 60, 84, 108]
    
    for prog in sorted(melodic_notes.keys()):
        for m_note in sorted(melodic_notes[prog]):
            # Find closest base note
            best_base = 60
            min_dist = 999
            for b in base_note_options:
                if abs(m_note - b) < min_dist:
                    min_dist = abs(m_note - b)
                    best_base = b
            
            key = (prog[0], prog[1], best_base)
            if key not in melodic_sample_map:
                all_instruments.append((prog[0], prog[1], best_base, False))
                melodic_sample_map[key] = len(all_instruments)
        
    for d_note in sorted(drum_notes_used):
        all_instruments.append((128, 0, d_note, True))
        drum_sample_map[d_note] = len(all_instruments)
        
    if not all_instruments:
        all_instruments.append((0, 0, 60, False))

    initial_bpm = get_initial_bpm(mid)

    print(f"Loading SF2 and rendering samples...")
    fs = FluidSynth(sf2_path)
    samples = []
    for bank, prog, note, is_drum in all_instruments:
        name = f"Drum {note}" if is_drum else f"Instr {prog}@{note}"
        print(f"  Recording {name}...")
        data = fs.render_sample(bank, prog, note=note)
        samples.append({'name': name, 'data': data})

    ticks_per_row = mid.ticks_per_beat // 4
    if ticks_per_row == 0: ticks_per_row = 1
    
    merged_track = mido.merge_tracks(mid.tracks)
    total_ticks = sum(msg.time for msg in merged_track)
    max_rows = int(total_ticks / ticks_per_row) + 128
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
            row_idx = int(abs_tick / ticks_per_row)
            if row_idx >= max_rows: continue
            
            if msg.channel == 9:
                instr_idx = drum_sample_map.get(msg.note, 1)
                note_to_play = 60 # Played at original pitch
            else:
                prog = current_channel_programs[msg.channel]
                # Find the sample used for this note
                best_base = 60
                min_dist = 999
                for b in base_note_options:
                    if abs(msg.note - b) < min_dist:
                        min_dist = abs(msg.note - b)
                        best_base = b
                
                instr_idx = melodic_sample_map.get((prog[0], prog[1], best_base), 1)
                # Formula: N = M - M_rec + 60
                note_to_play = msg.note - best_base + 60
            
            if note_to_play < 0: note_to_play = 0
            if note_to_play > 119: note_to_play = 119
            
            it_chan = msg.channel
            row_data[row_idx].append((it_chan, note_to_play, instr_idx, msg.velocity))

    # Pack patterns
    actual_last_row = 0
    for r_idx, rd in enumerate(row_data):
        if rd: actual_last_row = r_idx
    
    num_patterns = (actual_last_row + 64) // 64
    if num_patterns > 200: num_patterns = 200
    
    patterns = []
    print(f"Processing {num_patterns} patterns...")
    for p in range(num_patterns):
        p_bytes = bytearray()
        for r in range(64):
            row_idx = p * 64 + r
            notes_in_row = row_data[row_idx] if row_idx < len(row_data) else []
            
            seen_channels = {} # chan -> (note, instr, velocity)
            for it_chan, note, instr, velocity in notes_in_row:
                # If multiple notes on same channel, we'll try to find an empty IT channel (16-63)
                final_chan = it_chan
                if final_chan in seen_channels:
                    for c in range(16, 64):
                        if c not in seen_channels:
                            final_chan = c
                            break
                
                seen_channels[final_chan] = (note, instr, velocity)
                
            for it_chan, (note, instr, velocity) in sorted(seen_channels.items()):
                p_bytes.append((it_chan & 0x3F) + 1 | 0x80)
                p_bytes.append(0x07) # note + instr + vol
                p_bytes.append(note)
                p_bytes.append(instr)
                
                vol_it = int(velocity * 64 / 127)
                if vol_it < 0: vol_it = 0
                if vol_it > 64: vol_it = 64
                p_bytes.append(vol_it)
            
            p_bytes.append(0)
        patterns.append(bytes(p_bytes))

    orders = [i for i in range(num_patterns)]
    
    print(f"Writing IT file: {output_path}")
    write_it(output_path, os.path.basename(midi_path)[:26], samples, patterns, orders, initial_tempo=initial_bpm)
    print("Done!")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 midi2it.py <input.mid> <input.sf2> [output.it]")
    else:
        out = sys.argv[3] if len(sys.argv) > 3 else "output.it"
        try:
            convert_midi_to_it(sys.argv[1], sys.argv[2], out)
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
