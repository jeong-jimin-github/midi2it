# midi2it

midi2it is a Python tool for converting MIDI (.mid) files and SF2 soundfonts into Impulse Tracker (.it) module music files.

## Features

- Convert MIDI files to .it module format
- Support for SF2 soundfonts
- 100% Python implementation

## Requirements

- Python 3.7+
- See `requirements.txt` for Python dependencies (`mido`, `numpy`)
- **FluidSynth C library** (required for soundfont rendering; install separately, see below)

### Installing FluidSynth

The FluidSynth library must be installed on your system for this tool to work.

**macOS (using Homebrew):**
```bash
brew install fluidsynth
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install fluidsynth
```

**Windows:**  
Download and install the FluidSynth library DLL, and ensure it is accessible in your `PATH`. Refer to the official FluidSynth documentation for installation instructions.

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/jeong-jimin-github/midi2it.git
   cd midi2it
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

You need a MIDI file (`.mid`) and an SF2 soundfont to use this tool.

```bash
python midi2it.py input.mid input.sf2 [output.it]
```
- `input.mid`: Your MIDI file
- `input.sf2`: SoundFont file
- `output.it`: Output filename (optional, defaults to `output.it`)

## License

This project is licensed under the MIT License.

## Author

- [jeong-jimin-github](https://github.com/jeong-jimin-github)

---

_This tool is for converting MIDI + SF2 soundfont files into IT module music. Contributions and issues are welcome!_
