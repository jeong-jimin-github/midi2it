import contextlib
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Keep the GUI usable for source runs without the optional package.
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
        ttk.Entry(root, textvariable=self.output_var).grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Button(root, text="Browse...", command=self._browse_output).grid(row=3, column=2, padx=(8, 0), pady=5)

        self.log = scrolledtext.ScrolledText(root, height=14, state="disabled", wrap="word")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(12, 8))

        actions = ttk.Frame(root)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Label(actions, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.convert_button = ttk.Button(actions, text="Convert", command=self._start_conversion)
        self.convert_button.grid(row=0, column=1, sticky="e")

        self._register_drop_target(self.midi_entry, self._drop_midi)
        self._register_drop_target(self.soundfont_entry, self._drop_soundfont)

    def _register_drop_target(self, widget, handler):
        if DND_FILES is None:
            return
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", handler)

    def _dropped_paths(self, event):
        try:
            return self.tk.splitlist(event.data)
        except (AttributeError, tk.TclError):
            return ()

    def _set_midi_path(self, path):
        self.midi_var.set(path)
        if not self.output_var.get().strip():
            self.output_var.set(os.path.splitext(path)[0] + ".it")

    def _drop_midi(self, event):
        for path in self._dropped_paths(event):
            if os.path.isfile(path) and path.lower().endswith((".mid", ".midi")):
                self._set_midi_path(path)
                self.status_var.set("MIDI file dropped")
                return "break"
        self.status_var.set("Drop a .mid or .midi file")
        return "break"

    def _drop_soundfont(self, event):
        for path in self._dropped_paths(event):
            if os.path.isfile(path) and path.lower().endswith(".sf2"):
                self.soundfont_var.set(path)
                self.status_var.set("SoundFont dropped")
                return "break"
        self.status_var.set("Drop an .sf2 file")
        return "break"

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


def main():
    Midi2ItApp().mainloop()


if __name__ == "__main__":
    main()
