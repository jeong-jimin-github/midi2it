# midi2it

`midi2it` converts MIDI (`.mid`) files into Impulse Tracker (`.it`) modules by rendering the MIDI instruments through an SF2 SoundFont.

## Features

- MIDI to Impulse Tracker (`.it`) conversion
- CLI and Tkinter GUI
- SoundFont argument is optional: a GeneralUser GS SF2 is downloaded and cached automatically on first use
- Explicit custom SF2 SoundFonts are still supported
- MIDI time signatures such as 3/4, 6/8 and mid-song time-signature changes are reflected in IT pattern lengths
- Byte-identical rendered samples are deduplicated to reduce output size
- Windows EXE releases for both CLI and GUI, with FluidSynth bundled into each EXE

## Requirements

### Windows release EXEs

No separate FluidSynth installation is required. The release workflow bundles the official FluidSynth Windows x64 runtime and its required DLLs directly into both one-file executables.

### Running from Python source

- Python 3.7+
- Python packages in `requirements.txt` (`mido`, `numpy`)
- **FluidSynth C library** for SoundFont rendering

Install FluidSynth for source runs as appropriate for your platform:

**macOS**

```bash
brew install fluidsynth
```

**Debian / Ubuntu**

```bash
sudo apt-get install fluidsynth
```

**Windows source runs**

Install FluidSynth and make `fluidsynth.dll` / `libfluidsynth-*.dll` available in `PATH`, next to `midi2it.py`, or in a standard FluidSynth installation directory.

## Installation

```bash
git clone https://github.com/jeong-jimin-github/midi2it.git
cd midi2it
pip install -r requirements.txt
```

## CLI usage

The shortest form needs only a MIDI file:

```bash
python midi2it.py input.mid
```

On first use without an SF2 argument, midi2it downloads and caches GeneralUser GS automatically. The default output is `output.it`.

Choose an output path:

```bash
python midi2it.py input.mid -o song.it
```

Use your own SoundFont:

```bash
python midi2it.py input.mid --soundfont input.sf2 --output song.it
```

The old positional form remains supported:

```bash
python midi2it.py input.mid input.sf2 song.it
```

You can also set `MIDI2IT_SOUNDFONT` to use a preferred default SF2 without passing it each time.

## GUI usage

```bash
python midi2it_gui.py
```

Select a MIDI file, optionally select an SF2 file, choose the output path, and press **Convert**. Leaving the SoundFont field empty uses the same automatic download/cache behavior as the CLI.

## Windows releases

Each push builds and publishes:

- `midi2it.exe` — CLI
- `midi2it-gui.exe` — GUI

Both EXEs bundle the official FluidSynth v2.6.0 Windows x64 runtime (`libfluidsynth-3.dll`, `SDL3.dll`, and `sndfile.dll`) and extract it internally when launched. Users do not need to install FluidSynth separately.

## Default SoundFont

When no SoundFont is supplied, midi2it downloads the upstream **GeneralUser GS** SoundFont from the `mrbumpy409/GeneralUser-GS` repository and stores it in the local cache. The SoundFont itself is not bundled into this repository or EXE.

## Third-party components

Windows EXE builds include FluidSynth and runtime dependencies from the official FluidSynth Windows release. See `THIRD_PARTY_NOTICES.md` for attribution and license information.

## Tests

```bash
python -m unittest -v
```

## License

This project is licensed under the MIT License.

## Author

- [jeong-jimin-github](https://github.com/jeong-jimin-github)
