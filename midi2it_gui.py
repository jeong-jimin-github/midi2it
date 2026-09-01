import contextlib
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from urllib.parse import unquote, urlparse

try:
    from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD
except ImportError:  # Keep the GUI usable for source runs without the optional package.
    COPY = "copy"
    REFUSE_DROP = "refuse_drop"
    DND_FILES = None
    TkinterDnD = None

from midi2it import convert_midi_to_it


_TkBase = TkinterDnD.Tk if TkinterDnD is not None else tk.Tk


def _output_dialog_defaults(output_path, midi_path):
    current = (output_path or "").strip()
    midi = (midi_path or "").strip()
    if current:
        return os.path.dirname(os.path.abspath(current)), os.path.basename(current)
    if midi:
        return os.path.dirname(os.path.abspath(midi)), os.path.splitext(os.path.basename(midi))[0] + ".it"
    return None, None


def _normalize_dropped_path(path):
    path = str(path or "").strip()
    if path.lower().startswith("file://"):
        parsed = urlparse(path)
        path = unquote(parsed.path)
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
    return os.path.normpath(path)


def _drop_path_kind(path):
    lower = _normalize_dropped_path(path).lower()
    if lower.endswith((".mid", ".midi")):
        return "midi"
    if lower.endswith(".sf2"):
        return "soundfont"
    return None


class _GuiWriter:
    def __init__(self, root, text_widget):
        self.root = root
        self.text_widget = text_widget

    def write(self, text):
        if not text:
            return 0
        self.root.after(0, self._append, text)
        return len(text)

    def flush(self):
        pass

    def _append(self, text):
        self.text_widget.configure(state="normal")
        self.text_widget.insert("end", text)
        self.text_widget.see("end")
        self.text_widget.configure(state="disabled")


class Midi2ItApp(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("midi2it")
        self.minsize(680, 430)
        self.geometry("760x500")

        self.midi_var = tk.StringVar()
        self.soundfont_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self._build_ui()

    def _build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(4, weight=1)

        ttk.Label(root, text="MIDI file").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
        self.midi_entry = ttk.Entry(root, textvariable=self.midi_var)
        self.midi_entry.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(root, text="Browse...", command=self._browse_midi).grid(row=0, column=2, padx=(8, 0), pady=5)

        ttk.Label(root, text="SoundFont (optional)").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
        self.soundfont_entry = ttk.Entry(root, textvariable=self.soundfont_var)
        self.soundfont_entry.grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Button(root, text="Browse...", command=self._browse_soundfont).grid(row=1, column=2, padx=(8, 0), pady=5)

        hint = "Leave SoundFont empty to automatically download and cache GeneralUser GS."
        ttk.Label(root, text=hint).grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 5))

        ttk.Label(root, text="Output IT file").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=5)
        self.output_entry = ttk.Entry(root, textvariable=self.output_var)
        self.output_entry.grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Button(root, text="Browse...", command=self._browse_output).grid(row=3, column=2, padx=(8, 0), pady=5)

        self.log = scrolledtext.ScrolledText(root, height=14, state="disabled", wrap="word")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(12, 8))

        actions = ttk.Frame(root)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Label(actions, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.convert_button = ttk.Button(actions, text="Convert", command=self._start_conversion)
        self.convert_button.grid(row=0, column=1, sticky="e")

        # Entry-specific targets keep the intent obvious, while the frame/log/output
        # targets make file dropping work even when the pointer is not exactly over
        # one of the input fields.
        self._register_drop_target(self.midi_entry, self._drop_midi)
        self._register_drop_target(self.soundfont_entry, self._drop_soundfont)
        for widget in (self, root, self.output_entry, self.log, actions):
            self._register_drop_target(widget, self._drop_any)

    def _register_drop_target(self, widget, handler):
        if DND_FILES is None or not hasattr(widget, "drop_target_register"):
            return
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<DropEnter>>", self._allow_drop)
        widget.dnd_bind("<<DropPosition>>", self._allow_drop)
        widget.dnd_bind("<<Drop>>", handler)

    def _allow_drop(self, event):
        # tkDnD requires drop callbacks to return a valid DnD action. Returning
        # tkinter's normal "break" token is not valid here and can cause Windows
        # OLE drag/drop to reject the operation.
        return COPY

    def _dropped_paths(self, event):
        try:
            raw_paths = self.tk.splitlist(event.data)
        except (AttributeError, tk.TclError):
            return ()
        return tuple(_normalize_dropped_path(path) for path in raw_paths)

    def _set_midi_path(self, path):
        self.midi_var.set(path)
        if not self.output_var.get().strip():
            self.output_var.set(os.path.splitext(path)[0] + ".it")

    def _drop_midi(self, event):
        for path in self._dropped_paths(event):
            if os.path.isfile(path) and _drop_path_kind(path) == "midi":
                self._set_midi_path(path)
                self.status_var.set("MIDI file dropped")
                return COPY
        self.status_var.set("Drop a .mid or .midi file")
        return REFUSE_DROP

    def _drop_soundfont(self, event):
        for path in self._dropped_paths(event):
            if os.path.isfile(path) and _drop_path_kind(path) == "soundfont":
                self.soundfont_var.set(path)
                self.status_var.set("SoundFont dropped")
                return COPY
        self.status_var.set("Drop an .sf2 file")
        return REFUSE_DROP

    def _drop_any(self, event):
        accepted = []
        for path in self._dropped_paths(event):
            if not os.path.isfile(path):
                continue
            kind = _drop_path_kind(path)
            if kind == "midi":
                self._set_midi_path(path)
                accepted.append("MIDI")
            elif kind == "soundfont":
                self.soundfont_var.set(path)
                accepted.append("SoundFont")
        if accepted:
            self.status_var.set("Dropped: " + ", ".join(dict.fromkeys(accepted)))
            return COPY
        self.status_var.set("Drop a MIDI (.mid/.midi) or SoundFont (.sf2) file")
        return REFUSE_DROP

    def _browse_midi(self):
        path = filedialog.askopenfilename(title="Select MIDI file", filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")])
        if path:
            self._set_midi_path(path)

    def _browse_soundfont(self):
        path = filedialog.askopenfilename(title="Select SoundFont", filetypes=[("SoundFont 2", "*.sf2"), ("All files", "*.*")])
        if path:
            self.soundfont_var.set(path)

    def _browse_output(self):
        initial_dir, initial_file = _output_dialog_defaults(self.output_var.get(), self.midi_var.get())
        options = {
            "title": "Save IT file",
            "defaultextension": ".it",
            "filetypes": [("Impulse Tracker", "*.it"), ("All files", "*.*")],
        }
        if initial_dir:
            options["initialdir"] = initial_dir
        if initial_file:
            options["initialfile"] = initial_file
        path = filedialog.asksaveasfilename(**options)
        if path:
            self.output_var.set(path)

    def _start_conversion(self):
        midi_path = self.midi_var.get().strip()
        soundfont = self.soundfont_var.get().strip() or None
        output_path = self.output_var.get().strip()
        if not midi_path or not os.path.isfile(midi_path):
            messagebox.showerror("midi2it", "Select a valid MIDI file.")
            return
        if soundfont and not os.path.isfile(soundfont):
            messagebox.showerror("midi2it", "The selected SoundFont file does not exist.")
            return
        if not output_path:
            output_path = os.path.splitext(midi_path)[0] + ".it"
            self.output_var.set(output_path)

        self.convert_button.configure(state="disabled")
        self.status_var.set("Converting...")
        self._clear_log()
        threading.Thread(target=self._run_conversion, args=(midi_path, soundfont, output_path), daemon=True).start()

    def _run_conversion(self, midi_path, soundfont, output_path):
        writer = _GuiWriter(self, self.log)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                convert_midi_to_it(midi_path, soundfont, output_path)
            self.after(0, self._conversion_done, output_path)
        except Exception as exc:
            writer.write(f"\nError: {exc}\n")
            self.after(0, self._conversion_failed, str(exc))

    def _conversion_done(self, output_path):
        self.convert_button.configure(state="normal")
        self.status_var.set("Done")
        messagebox.showinfo("midi2it", f"Conversion completed.\n\n{output_path}")

    def _conversion_failed(self, error):
        self.convert_button.configure(state="normal")
        self.status_var.set("Failed")
        messagebox.showerror("midi2it", error)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def _check_dnd_runtime(marker_path):
    marker = Path(marker_path)
    try:
        if TkinterDnD is None or DND_FILES is None:
            raise RuntimeError("tkinterdnd2 is not available")
        app = Midi2ItApp()
        app.withdraw()
        try:
            version = app.tk.call("package", "require", "tkdnd")
            if not version:
                raise RuntimeError("tkdnd package did not report a version")
            if not hasattr(app.midi_entry, "drop_target_register"):
                raise RuntimeError("drop_target_register is unavailable")
        finally:
            app.destroy()
        marker.write_text(f"OK tkdnd={version}\n", encoding="utf-8")
        return 0
    except Exception as exc:
        marker.write_text(f"ERROR {type(exc).__name__}: {exc}\n", encoding="utf-8")
        return 1


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--check-dnd":
        marker = args[1] if len(args) > 1 else "dnd-check.txt"
        return _check_dnd_runtime(marker)
    Midi2ItApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
