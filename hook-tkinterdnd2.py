from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# tkinterdnd2 wraps the native tkdnd Tcl/Tk extension. Keep its platform
# runtime files next to the packaged Python module so Tk can `package require`
# tkdnd inside a PyInstaller one-file extraction directory.
datas = collect_data_files("tkinterdnd2")
binaries = collect_dynamic_libs("tkinterdnd2")
hiddenimports = ["tkinterdnd2"]
