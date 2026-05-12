# midi2it

midi2it is a Python tool for converting MIDI (.mid) files and SF2 soundfonts into Impulse Tracker (.it) module music files.

## Features

- Convert MIDI files to .it module format
- Support for SF2 soundfonts
- 100% Python implementation

## Usage

1. Clone this repository:
   ```bash
   git clone https://github.com/jeong-jimin-github/midi2it.git
   cd midi2it
   ```

2. Install requirements (if any are required; see requirements.txt if it exists):
   ```bash
   pip install -r requirements.txt
   ```

3. Run the tool to convert a MIDI file:
   ```bash
   python midi2it.py input.mid input.sf2 output.it
   ```
   Replace `input.mid` with your MIDI file, `input.sf2` with your SF2 soundfont, and `output.it` with the desired output file name.

## Requirements

- Python 3.7+
- Additional dependencies may be specified in `requirements.txt`

## License

This project is licensed under the MIT License.

## Author

- [jeong-jimin-github](https://github.com/jeong-jimin-github)

---

_This tool is for converting MIDI + SF2 soundfont files into IT module music. Contributions and issues are welcome!_
