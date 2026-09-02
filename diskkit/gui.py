#!/usr/bin/env python3
r"""Disk Cleaner & Info -- an Aura (QuickOpen design system) GUI over ``diskkit``.

A single Aura window: the sidebar lists the four sections (Clean, Large Files,
Disk Usage, System Monitor) and the main panel swaps to the selected section.
Every operation calls the tested core library (never re-implements the logic)
and runs on a background thread so the UI stays responsive; results are
marshalled back with ``self.after`` and shown in the Aura status bar -- a
summary line on success, or the :class:`DiskKitError` message (never a raw
traceback) on failure.

Design goals baked in here (mirrors the QuickOpen house style):
  * built on the vendored ``diskkit/aura.py`` design system, which layers the
    quickopen.ai look (deep space + light) over CustomTkinter.  Runtime deps:
    ``customtkinter`` (+ ``darkdetect``) -- declared in requirements.txt; the
    PyInstaller build adds ``--collect-all customtkinter``.
  * Importing this module does nothing.  Only :func:`main` builds a root
    window, and it degrades gracefully (prints a message, returns 0) with no
    display or with customtkinter missing.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the
    exe directory when ``sys.frozen`` is set -- never ``__file__``.
  * Destructive actions (Clean, delete large files, empty Recycle Bin) always
    confirm first, and default to the Recycle Bin (recoverable).

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so
# that merely importing this module (packaging, headless CI) never fails.

APP_NAME = "Disk Cleaner & Info"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "Disk Cleaner & Info — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#2f5fe0"      # UI-accent registry: disk-cleaner -> #2f5fe0

# (section_id, label, DejaVu-safe nav glyph) -- id maps to a _build_<id> method.
VIEWS = [
    ("clean", "Clean", "✳"),
    ("large", "Large Files", "▤"),
    ("usage", "Disk Usage", "⛁"),
    ("monitor", "System Monitor", "◉"),
]

VIEW_DESCRIPTIONS = {
    "clean": "Scan temporary files, browser caches and other junk, then reclaim "
             "the space. Deletions go to the Recycle Bin by default.",
    "large": "Find the biggest files under a folder and send the ones you pick "
             "to the Recycle Bin.",
    "usage": "See which sub-folders are using the most space under a folder.",
    "monitor": "Live CPU, memory, disk and network usage, plus the top processes.",
}


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
def asset_path(name):
    """Locate a bundled asset from source OR a PyInstaller one-file build.

    For a frozen exe we look only at ``sys._MEIPASS`` and the executable's own
    directory (never ``__file__``).  From source we also consult the package
    dir, the repo root and the CWD.  Returns an absolute path or ``None``.
    """
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots += [here, os.path.dirname(here), os.getcwd()]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def human_size(num_bytes):
    """Human-readable byte size (re-uses the core helper when available)."""
    try:
        from .common import human_size as _hs
        return _hs(num_bytes)
    except Exception:
        size = float(num_bytes or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
            size /= 1024.0
        return f"{size:.1f}TB"


def open_in_file_manager(path):
    """Best-effort 'reveal in file manager', guarded on every platform."""
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if hasattr(os, "startfile"):          # Windows
            os.startfile(folder)              # noqa: S606 - intended
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", folder])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The app (built lazily; tkinter/customtkinter imported only inside build_app)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to live GUI imports.

    Kept inside a function so this module imports cleanly without a display
    (and without customtkinter installed).
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from .aura import filedialog  # noqa: F811 - Aura kdialog-native pickers
    import customtkinter as ctk

    from . import aura, guiconfig
    from .errors import DiskKitError
    from .cleaners import (
        TARGETS, scan_targets, clean_targets, empty_recycle_bin,
    )
    from .largefiles import find_large_files, dir_sizes
    from . import sysinfo, monitor  # noqa: F401 (sysinfo kept for parity)

    # ---- reusable folder picker row ------------------------------------
    class FolderRow(ctk.CTkFrame):
        """A labelled 'folder + Browse' row that remembers recent picks."""

        def __init__(self, master, app, label, on_change=None):
            super().__init__(master, fg_color="transparent")
            self.app = app
            self.on_change = on_change
            aura.SectionLabel(self, label).pack(anchor="w", pady=(0, 3))
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x")
            # no textvariable: CTkEntry placeholders only work without one
            self.entry = aura.AuraEntry(row, placeholder="Choose a folder…")
            self.entry.pack(side="left", fill="x", expand=True)
            aura.AuraButton(row, "Browse…", kind="secondary",
                            command=self._browse).pack(side="left", padx=(8, 0))

        def _browse(self):
            initial = self.get() or os.path.expanduser("~")
            chosen = filedialog.askdirectory(initialdir=initial, mustexist=True)
            if chosen:
                self.set(chosen)

        def get(self):
            return self.entry.get().strip()

        def set(self, value):
            self.entry.delete(0, "end")
            if value:
                self.entry.insert(0, value)
                guiconfig.add_recent(value)
            if self.on_change:
                self.on_change(value)

    class App(aura.AuraApp):
        def __init__(self):
            super().__init__(
                title=WINDOW_TITLE, app_name=APP_NAME, accent=ACCENT,
                theme=guiconfig.get_theme(),
                icon_png=asset_path("disk-cleaner.png"), version=APP_VERSION,
                tagline="offline cleanup",
                on_theme_change=guiconfig.set_theme,
                size=(1080, 700), min_size=(900, 600))

            self._busy = False
            self._img_refs_gui = []    # keep PhotoImage refs alive

            # Clean view state
            self._clean_vars = {}      # target_id -> BooleanVar
            self._clean_scan = {}      # target_id -> scan info
            # Large files state
            self._large_results = []
            self._large_sort = ("bytes", True)
            # Monitor state
            self._mon_running = False
            self._mon_thread = None
            self._mon_prev_net = None
            self._core_bars = []

            self._set_icon()
            self._build_menu()

            # Shimmer progress lives in the status bar while an op runs.
            self.progress = aura.ProgressBar(self.statusbar.actions,
                                             mode="indeterminate", width=140)

            for vid, label, glyph in VIEWS:
                self.add_section(vid, label, glyph,
                                 getattr(self, "_build_" + vid))
            self.show("clean")
            self.set_status("Ready")
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("disk-cleaner.ico")
                if ico and os.name == "nt":
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("disk-cleaner.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs_gui.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- menu (native menus stay; theme lives in the sidebar toggle too)
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)
            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)
            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About",
                              command=lambda: messagebox.showinfo(
                                  "About " + APP_NAME,
                                  f"{APP_NAME} {APP_VERSION}\n\n"
                                  "Offline, open-source disk cleaner & system "
                                  "monitor.\nDeletions go to the Recycle Bin by "
                                  "default.\n\nBuilt by QuickOpen — quickopen.ai"))
            bar.add_cascade(label="Help", menu=helpm)
            try:
                self.config(menu=bar)
            except Exception:
                pass

        # ---- navigation (leaving the monitor stops its background sampling)
        def show(self, sid):
            if self.active_section == "monitor" and sid != "monitor":
                self._stop_monitor()
            super().show(sid)
            self.set_status("Ready")

        # ---- background operation runner
        def _bg(self, work, on_ok, button=None, busy="Working…"):
            """Run ``work()`` off the UI thread; call ``on_ok(result)`` back on it."""
            if self._busy:
                self._show_error("Please wait — an operation is already running.")
                return
            self._busy = True
            if button is not None:
                try:
                    button.state(["disabled"])
                except Exception:
                    pass
            self._set_status(busy, kind="working")
            try:
                self.progress.pack(side="left", padx=(8, 4), pady=6)
                self.progress.start()
            except Exception:
                pass

            def run():
                try:
                    res, err = work(), None
                except DiskKitError as ex:
                    res, err = None, str(ex)
                except Exception as ex:  # never leak a traceback
                    res, err = None, f"Unexpected error: {ex}"
                self.after(0, lambda: finish(res, err))

            def finish(res, err):
                self._busy = False
                try:
                    self.progress.stop()
                    self.progress.pack_forget()
                except Exception:
                    pass
                if button is not None:
                    try:
                        button.state(["!disabled"])
                    except Exception:
                        pass
                if err is not None:
                    self._show_error(err)
                    return
                self._set_status("Done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- status helpers (Aura status bar is the voice of the app)
        def _set_status(self, text, kind="idle"):
            self.set_status(text, kind)

        def _show_error(self, message):
            self.set_error(message)

        def report_success(self, message):
            self.set_success(message)

        # ===============================================================
        # Clean section
        # ===============================================================
        def _build_clean(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["clean"],
                         wraplength=760, justify="left").pack(
                anchor="w", pady=(0, 10))
            top = ctk.CTkFrame(frame, fg_color="transparent")
            top.pack(fill="x")
            self._clean_scan_btn = aura.AuraButton(
                top, "Scan for junk", kind="primary",
                command=self._clean_do_scan)
            self._clean_scan_btn.pack(side="left")
            self._clean_perm = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(top, text="Delete permanently (skip Recycle Bin)",
                            variable=self._clean_perm,
                            font=aura.font()).pack(side="left", padx=14)
            # Quick OS 0.6.0 (Storage Sense parity): a weekly, unattended
            # clean of every target INTO the Recycle Bin (recoverable), run by
            # a per-user systemd timer. Linux only; off unless the user ticks it.
            if _timer_supported():
                self._clean_auto = tk.BooleanVar(value=_timer_enabled())
                ctk.CTkCheckBox(top, text="Clean automatically every week (to the Recycle Bin)",
                                variable=self._clean_auto, command=self._clean_toggle_auto,
                                font=aura.font()).pack(side="left", padx=14)

        def _clean_toggle_auto(self):
            from tkinter import messagebox
            try:
                _timer_set(bool(self._clean_auto.get()))
            except Exception as exc:  # noqa: BLE001
                self._clean_auto.set(_timer_enabled())
                messagebox.showerror("Disk Cleaner", f"Could not change the weekly clean: {exc}")

            mid = aura.Card(frame, title="Cleanup targets", padding=12)
            mid.pack(fill="both", expand=True, pady=12)
            self._clean_list = ctk.CTkScrollableFrame(mid.body,
                                                      fg_color="transparent")
            self._clean_list.pack(fill="both", expand=True)

            aura.Caption(self._clean_list,
                         "Click “Scan for junk” to see what can be cleaned.").pack(
                anchor="w", padx=4, pady=8)

            bottom = ctk.CTkFrame(frame, fg_color="transparent")
            bottom.pack(fill="x")
            self._clean_total = ctk.CTkLabel(bottom, text="Reclaimable: —",
                                             font=aura.font(15, "bold"),
                                             anchor="w")
            self._clean_total.pack(side="left")
            self._clean_btn = aura.AuraButton(
                bottom, "Clean selected", kind="danger",
                command=self._clean_do_clean)
            self._clean_btn.pack(side="right")
            try:
                self._clean_btn.state(["disabled"])
            except Exception:
                pass

        def _clean_do_scan(self):
            def work():
                return scan_targets(None)

            def done(report):
                self._clean_scan = report
                for child in list(self._clean_list.winfo_children()):
                    child.destroy()
                self._clean_vars = {}
                total = 0
                for target in TARGETS:
                    info = report.get(target.id, {})
                    if target.special:
                        self._clean_add_special(target, info)
                        continue
                    files = info.get("files", 0)
                    nbytes = info.get("bytes", 0)
                    total += nbytes
                    var = tk.BooleanVar(value=bool(files))
                    self._clean_vars[target.id] = var
                    row = ctk.CTkFrame(self._clean_list,
                                       fg_color="transparent")
                    row.pack(fill="x", padx=4, pady=3)
                    cb = ctk.CTkCheckBox(
                        row, variable=var, font=aura.font(),
                        text=f"{target.name}  —  {human_size(nbytes)} "
                             f"({files} files)")
                    cb.pack(side="left", anchor="w")
                    if not files:
                        try:
                            cb.configure(state="disabled")
                        except Exception:
                            pass
                    aura.Caption(row, target.description,
                                 wraplength=460, justify="left").pack(
                        side="left", padx=12)
                self._clean_total.configure(
                    text=f"Reclaimable: {human_size(total)}")
                try:
                    self._clean_btn.state(
                        ["!disabled"] if total else ["disabled"])
                except Exception:
                    pass
                self.report_success(
                    f"Scan complete — {human_size(total)} reclaimable.")

            self._bg(work, done, button=self._clean_scan_btn, busy="Scanning…")

        def _clean_add_special(self, target, info):
            row = ctk.CTkFrame(self._clean_list, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=3)
            aura.Caption(row, f"{target.name} — {target.description}",
                         wraplength=560, justify="left").pack(
                side="left", anchor="w")
            btn = aura.AuraButton(row, "Empty Recycle Bin…", kind="secondary",
                                  command=self._clean_empty_bin)
            btn.pack(side="right")
            if os.name != "nt":
                try:
                    btn.state(["disabled"])
                except Exception:
                    pass

        def _clean_empty_bin(self):
            if not messagebox.askyesno(
                    "Empty Recycle Bin",
                    "Permanently empty the Recycle Bin?\nThis cannot be undone."):
                return
            self._bg(lambda: empty_recycle_bin(confirm=True),
                     lambda _r: self.report_success("Recycle Bin emptied."))

        def _clean_do_clean(self):
            selected = [tid for tid, var in self._clean_vars.items() if var.get()]
            if not selected:
                self._show_error("Select at least one item to clean.")
                return
            total = sum(self._clean_scan.get(t, {}).get("bytes", 0)
                        for t in selected)
            permanent = self._clean_perm.get()
            dest = ("permanently deleted" if permanent
                    else "sent to the Recycle Bin")
            if not messagebox.askyesno(
                    "Confirm clean",
                    f"Clean {len(selected)} location(s), freeing about "
                    f"{human_size(total)}?\n\nFiles will be {dest}."):
                return

            def work():
                return clean_targets(selected, to_trash=not permanent)

            def done(freed):
                self.report_success(f"Freed {human_size(freed)} ({dest}).")
                self._clean_do_scan()  # refresh sizes

            self._bg(work, done, button=self._clean_btn, busy="Cleaning…")

        # ===============================================================
        # Large Files section
        # ===============================================================
        def _build_large(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["large"],
                         wraplength=760, justify="left").pack(
                anchor="w", pady=(0, 10))
            self._large_folder = FolderRow(frame, self, "Folder to scan")
            self._large_folder.pack(fill="x", pady=4)
            opts = ctk.CTkFrame(frame, fg_color="transparent")
            opts.pack(fill="x", pady=8)
            ctk.CTkLabel(opts, text="Minimum size:",
                         font=aura.font()).pack(side="left")
            self._large_min = aura.AuraCombo(
                opts, width=110,
                values=["1MB", "10MB", "50MB", "100MB", "500MB", "1GB"])
            self._large_min.set("10MB")
            self._large_min.pack(side="left", padx=(6, 16))
            ctk.CTkLabel(opts, text="Max results:",
                         font=aura.font()).pack(side="left")
            self._large_limit = ttk.Spinbox(opts, from_=10, to=1000, width=6)
            self._large_limit.set(100)
            self._large_limit.pack(side="left", padx=(6, 16))
            self._large_find_btn = aura.AuraButton(
                opts, "Find large files", kind="primary",
                command=self._large_do_find)
            self._large_find_btn.pack(side="left")

            table = ctk.CTkFrame(frame, fg_color="transparent")
            table.pack(fill="both", expand=True, pady=8)
            cols = ("size", "path")
            self._large_tree = ttk.Treeview(
                table, columns=cols, show="headings", selectmode="extended")
            self._large_tree.heading(
                "size", text=aura.spaced("Size ▾"), anchor="w",
                command=lambda: self._large_sort_by("bytes"))
            self._large_tree.heading(
                "path", text=aura.spaced("Path"), anchor="w",
                command=lambda: self._large_sort_by("path"))
            self._large_tree.column("size", width=110, anchor="e", stretch=False)
            self._large_tree.column("path", width=640, anchor="w")
            sb = ttk.Scrollbar(table, orient="vertical",
                               command=self._large_tree.yview)
            self._large_tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._large_tree.pack(side="left", fill="both", expand=True)

            actions = ctk.CTkFrame(frame, fg_color="transparent")
            actions.pack(fill="x")
            self._large_del_btn = aura.AuraButton(
                actions, "Send selected to Recycle Bin", kind="danger",
                command=self._large_delete)
            self._large_del_btn.pack(side="left")
            aura.AuraButton(actions, "Open containing folder", kind="secondary",
                            command=self._large_reveal).pack(
                side="left", padx=10)
            self._large_perm = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(actions, text="Delete permanently",
                            variable=self._large_perm,
                            font=aura.font()).pack(side="left", padx=10)

        def _large_do_find(self):
            root = self._large_folder.get()
            if not root:
                self._show_error("Choose a folder to scan.")
                return
            from .__main__ import _parse_size
            try:
                min_bytes = _parse_size(self._large_min.get())
                limit = int(self._large_limit.get())
            except Exception:
                min_bytes, limit = 0, 100

            def work():
                return find_large_files(root, min_bytes=min_bytes, limit=limit)

            def done(results):
                self._large_results = results
                self._large_sort = ("bytes", True)
                self._large_render()
                self.report_success(f"Found {len(results)} file(s).")

            self._bg(work, done, button=self._large_find_btn, busy="Scanning…")

        def _large_sort_by(self, key):
            cur_key, desc = self._large_sort
            desc = (not desc) if cur_key == key else True
            self._large_sort = (key, desc)
            self._large_render()

        def _large_render(self):
            tree = self._large_tree
            tree.delete(*tree.get_children())
            key, desc = self._large_sort
            if key == "path":
                data = sorted(self._large_results,
                              key=lambda d: d["path"].lower(), reverse=desc)
            else:
                data = sorted(self._large_results,
                              key=lambda d: d["bytes"], reverse=desc)
            tree.heading("size", text=aura.spaced(
                "Size " + ("▾" if desc else "▴") if key == "bytes" else "Size"))
            tree.heading("path", text=aura.spaced(
                "Path " + ("▾" if desc else "▴") if key == "path" else "Path"))
            for item in data:
                tree.insert("", "end", iid=item["path"],
                            values=(human_size(item["bytes"]), item["path"]))

        def _large_selected_paths(self):
            return [p for p in self._large_tree.selection() if os.path.exists(p)]

        def _large_reveal(self):
            paths = self._large_selected_paths()
            if paths:
                open_in_file_manager(paths[0])

        def _large_delete(self):
            paths = self._large_selected_paths()
            if not paths:
                self._show_error("Select one or more files first.")
                return
            permanent = self._large_perm.get()
            dest = "permanently deleted" if permanent else "sent to the Recycle Bin"
            total = 0
            for p in paths:
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
            if not messagebox.askyesno(
                    "Confirm delete",
                    f"{len(paths)} file(s) ({human_size(total)}) will be {dest}."):
                return
            from .cleaners import _delete_path

            def work():
                freed = 0
                for p in paths:
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        size = 0
                    try:
                        _delete_path(p, to_trash=not permanent)
                        freed += size
                    except Exception:
                        continue
                return freed

            def done(freed):
                self._large_results = [
                    d for d in self._large_results if os.path.exists(d["path"])]
                self._large_render()
                self.report_success(f"Deleted — freed {human_size(freed)} ({dest}).")

            self._bg(work, done, button=self._large_del_btn, busy="Deleting…")

        # ===============================================================
        # Disk Usage section
        # ===============================================================
        def _build_usage(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["usage"],
                         wraplength=760, justify="left").pack(
                anchor="w", pady=(0, 10))
            self._usage_folder = FolderRow(frame, self, "Folder to analyse")
            self._usage_folder.pack(fill="x", pady=4)
            opts = ctk.CTkFrame(frame, fg_color="transparent")
            opts.pack(fill="x", pady=8)
            ctk.CTkLabel(opts, text="Depth:", font=aura.font()).pack(side="left")
            self._usage_depth = ttk.Spinbox(opts, from_=1, to=6, width=5)
            self._usage_depth.set(2)
            self._usage_depth.pack(side="left", padx=(6, 16))
            self._usage_btn = aura.AuraButton(
                opts, "Analyse usage", kind="primary", command=self._usage_do)
            self._usage_btn.pack(side="left")

            table = ctk.CTkFrame(frame, fg_color="transparent")
            table.pack(fill="both", expand=True, pady=8)
            self._usage_tree = ttk.Treeview(
                table, columns=("size", "pct", "files"), show="tree headings")
            self._usage_tree.heading("#0", text=aura.spaced("Folder"),
                                     anchor="w")
            self._usage_tree.heading("size", text=aura.spaced("Size"),
                                     anchor="w")
            self._usage_tree.heading("pct", text=aura.spaced("Share"),
                                     anchor="w")
            self._usage_tree.heading("files", text=aura.spaced("Files"),
                                     anchor="w")
            self._usage_tree.column("#0", width=380, anchor="w")
            self._usage_tree.column("size", width=110, anchor="e", stretch=False)
            self._usage_tree.column("pct", width=185, anchor="w", stretch=False)
            self._usage_tree.column("files", width=90, anchor="e", stretch=False)
            sb = ttk.Scrollbar(table, orient="vertical",
                               command=self._usage_tree.yview)
            self._usage_tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._usage_tree.pack(side="left", fill="both", expand=True)

        def _usage_do(self):
            root = self._usage_folder.get()
            if not root:
                self._show_error("Choose a folder to analyse.")
                return
            try:
                depth = int(self._usage_depth.get())
            except Exception:
                depth = 2

            def work():
                return dir_sizes(root, depth=depth)

            def done(tree):
                self._usage_render(tree)
                self.report_success(
                    f"{tree['name']}: {human_size(tree['bytes'])} "
                    f"in {tree['files']} files.")

            self._bg(work, done, button=self._usage_btn, busy="Analysing…")

        def _usage_render(self, node):
            tree = self._usage_tree
            tree.delete(*tree.get_children())
            total = node["bytes"] or 1

            def bar(nbytes):
                filled = int(round(12 * nbytes / total))
                return "█" * filled + "░" * (12 - filled)

            def insert(parent_iid, n, is_root=False):
                pct = 100.0 * n["bytes"] / total
                text = ("▸ " + n["name"]) if is_root else n["name"]
                iid = tree.insert(
                    parent_iid, "end", text=text, open=is_root,
                    values=(human_size(n["bytes"]),
                            f"{bar(n['bytes'])} {pct:4.1f}%",
                            n["files"]))
                for child in n.get("children", []):
                    insert(iid, child)

            insert("", node, is_root=True)

        # ===============================================================
        # System Monitor section
        # ===============================================================
        def _build_monitor(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["monitor"],
                         wraplength=760, justify="left").pack(
                anchor="w", pady=(0, 10))
            ctl = ctk.CTkFrame(frame, fg_color="transparent")
            ctl.pack(fill="x")
            self._mon_btn = aura.AuraButton(
                ctl, "▶ Start monitor", kind="primary",
                command=self._toggle_monitor)
            self._mon_btn.pack(side="left")
            aura.Caption(ctl, "updates live on a background thread").pack(
                side="left", padx=10)

            grid = ctk.CTkFrame(frame, fg_color="transparent")
            grid.pack(fill="both", expand=True, pady=10)

            # left column: CPU + memory dials
            left = aura.Card(grid, title="CPU / Memory", padding=14)
            left.pack(side="left", fill="both", expand=True, padx=(0, 7))
            self._mon_cpu_lbl = ctk.CTkLabel(left.body, text="CPU: —",
                                             font=aura.font(), anchor="w")
            self._mon_cpu_lbl.pack(anchor="w")
            self._mon_cpu_bar = aura.ProgressBar(left.body)
            self._mon_cpu_bar.pack(fill="x", pady=(4, 10))
            self._mon_cores = ctk.CTkFrame(left.body, fg_color="transparent",
                                           width=1, height=1)
            self._mon_cores.pack(fill="x")
            self._mon_cores.grid_columnconfigure((0, 1), weight=1)
            self._mon_mem_lbl = ctk.CTkLabel(left.body, text="Memory: —",
                                             font=aura.font(), anchor="w")
            self._mon_mem_lbl.pack(anchor="w", pady=(10, 0))
            self._mon_mem_bar = aura.ProgressBar(left.body)
            self._mon_mem_bar.pack(fill="x", pady=(4, 0))
            self._mon_swap_lbl = ctk.CTkLabel(left.body, text="Swap: —",
                                              font=aura.font(), anchor="w")
            self._mon_swap_lbl.pack(anchor="w", pady=(10, 0))
            self._mon_swap_bar = aura.ProgressBar(left.body)
            self._mon_swap_bar.pack(fill="x", pady=(4, 0))
            self._mon_net_lbl = aura.Caption(left.body, "Network: —")
            self._mon_net_lbl.pack(anchor="w", pady=(10, 0))
            self._mon_bat_lbl = aura.Caption(left.body, "")
            self._mon_bat_lbl.pack(anchor="w")

            # right column: disks + processes
            right = ctk.CTkFrame(grid, fg_color="transparent")
            right.pack(side="left", fill="both", expand=True, padx=(7, 0))
            dframe = aura.Card(right, title="Disks", padding=12)
            dframe.pack(fill="x")
            self._mon_disks = ttk.Treeview(
                dframe.body, columns=("used", "pct"), show="tree headings",
                height=5)
            self._mon_disks.heading("#0", text=aura.spaced("Mount"),
                                    anchor="w")
            self._mon_disks.heading("used", text=aura.spaced("Used / Total"),
                                    anchor="w")
            self._mon_disks.heading("pct", text=aura.spaced("%"), anchor="w")
            self._mon_disks.column("#0", width=140)
            self._mon_disks.column("used", width=180, anchor="e")
            self._mon_disks.column("pct", width=60, anchor="e")
            self._mon_disks.pack(fill="x")

            pframe = aura.Card(right, title="Top processes by memory",
                               padding=12)
            pframe.pack(fill="both", expand=True, pady=(10, 0))
            self._mon_proc = ttk.Treeview(
                pframe.body, columns=("pid", "mem", "cpu"),
                show="tree headings")
            self._mon_proc.heading("#0", text=aura.spaced("Process"),
                                   anchor="w")
            self._mon_proc.heading("pid", text=aura.spaced("PID"), anchor="w")
            self._mon_proc.heading("mem", text=aura.spaced("Memory"),
                                   anchor="w")
            self._mon_proc.heading("cpu", text=aura.spaced("CPU %"),
                                   anchor="w")
            self._mon_proc.column("#0", width=180)
            self._mon_proc.column("pid", width=70, anchor="e")
            self._mon_proc.column("mem", width=110, anchor="e")
            self._mon_proc.column("cpu", width=70, anchor="e")
            sb = ttk.Scrollbar(pframe.body, orient="vertical",
                               command=self._mon_proc.yview)
            self._mon_proc.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._mon_proc.pack(side="left", fill="both", expand=True)

        def _toggle_monitor(self):
            if self._mon_running:
                self._stop_monitor()
            else:
                self._start_monitor()

        def _start_monitor(self):
            if self._mon_running:
                return
            self._mon_running = True
            self._mon_prev_net = None
            self._mon_btn.configure(text="■ Stop monitor")
            self._set_status("Monitoring…", kind="working")

            def loop():
                for snap in monitor.sample(interval=1.0):
                    if not self._mon_running:
                        break
                    self.after(0, lambda s=snap: self._monitor_update(s))
                    if not self._mon_running:
                        break

            self._mon_thread = threading.Thread(target=loop, daemon=True)
            self._mon_thread.start()

        def _stop_monitor(self):
            self._mon_running = False
            try:
                self._mon_btn.configure(text="▶ Start monitor")
            except Exception:
                pass
            self._set_status("Ready")

        def _monitor_update(self, snap):
            if not self._mon_running:
                return
            try:
                cpu = snap["cpu"]
                self._mon_cpu_lbl.configure(
                    text=f"CPU: {cpu['percent']:.0f}%  "
                         f"({cpu.get('count_logical')} cores)")
                self._mon_cpu_bar.set(min(1.0, cpu["percent"] / 100.0))
                cores = cpu.get("per_core") or []
                if len(self._core_bars) != len(cores):
                    for w in self._mon_cores.winfo_children():
                        w.destroy()
                    self._core_bars = []
                    for i in range(len(cores)):
                        b = aura.ProgressBar(self._mon_cores, width=120)
                        b.grid(row=i // 2, column=i % 2, padx=4, pady=3,
                               sticky="we")
                        self._core_bars.append(b)
                for b, val in zip(self._core_bars, cores):
                    b.set(min(1.0, (val or 0) / 100.0))

                vm = snap["memory"].get("virtual") or {}
                self._mon_mem_lbl.configure(
                    text=f"Memory: {vm.get('percent', 0):.0f}%  "
                         f"({human_size(vm.get('used', 0))} / "
                         f"{human_size(vm.get('total', 0))})")
                self._mon_mem_bar.set(min(1.0, vm.get("percent", 0) / 100.0))
                sw = snap["memory"].get("swap") or {}
                self._mon_swap_lbl.configure(
                    text=f"Swap: {sw.get('percent', 0):.0f}%  "
                         f"({human_size(sw.get('used', 0))} / "
                         f"{human_size(sw.get('total', 0))})")
                self._mon_swap_bar.set(min(1.0, sw.get("percent", 0) / 100.0))

                net = snap.get("net") or {}
                if self._mon_prev_net is not None:
                    dr = net.get("bytes_recv", 0) - self._mon_prev_net.get("bytes_recv", 0)
                    ds = net.get("bytes_sent", 0) - self._mon_prev_net.get("bytes_sent", 0)
                    self._mon_net_lbl.configure(
                        text=f"Network: ↓ {human_size(max(0, dr))}/s   "
                             f"↑ {human_size(max(0, ds))}/s")
                self._mon_prev_net = net

                bat = snap.get("battery")
                if bat:
                    plug = "charging" if bat["plugged_in"] else "on battery"
                    self._mon_bat_lbl.configure(
                        text=f"Battery: {bat['percent']:.0f}% ({plug})")

                self._mon_disks.delete(*self._mon_disks.get_children())
                for d in snap["disks"]["partitions"]:
                    self._mon_disks.insert(
                        "", "end", text=d["mountpoint"],
                        values=(f"{human_size(d['used'])} / {human_size(d['total'])}",
                                f"{d['percent']:.0f}"))
                self._mon_proc.delete(*self._mon_proc.get_children())
                for pr in snap["top_memory"]:
                    self._mon_proc.insert(
                        "", "end", text=(pr["name"] or "")[:28],
                        values=(pr["pid"], human_size(pr.get("memory_rss") or 0),
                                f"{pr.get('cpu_percent') or 0:.0f}"))
            except Exception:
                pass  # a single bad frame must never crash the UI

        # ---- lifecycle
        def _on_close(self):
            self._stop_monitor()
            try:
                self.destroy()
            except Exception:
                pass

    return App


def main():
    """Entry point: build the root window and run.  Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server) or without customtkinter installed, it
    prints a friendly note and returns 0 instead of raising.
    """
    try:
        import tkinter as tk
    except Exception as exc:  # tkinter missing entirely
        print(f"{APP_NAME}: a graphical environment with tkinter is required "
              f"to run the GUI ({exc}).")
        return 0

    try:
        App = build_app()
        app = App()
    except ImportError as exc:
        print(f"{APP_NAME}: the GUI needs the 'customtkinter' package "
              f"({exc}). Install it with:  pip install customtkinter")
        return 0
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}). This app is intended for the Windows desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ---- Quick OS 0.6.0: weekly automatic clean (per-user systemd timer) -------
_TIMER = "diskkit-weekly-clean"


def _timer_supported():
    import shutil, sys
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def _timer_dir():
    import os
    d = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                     "systemd", "user")
    os.makedirs(d, exist_ok=True)
    return d


def _timer_enabled():
    import subprocess
    try:
        r = subprocess.run(["systemctl", "--user", "is-enabled", _TIMER + ".timer"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "enabled"
    except Exception:
        return False


def _timer_set(on):
    import os, shutil, subprocess, sys
    d = _timer_dir()
    exe = shutil.which("quickopen-disk-cleaner") or f"{sys.executable} -m diskkit"
    if on:
        with open(os.path.join(d, _TIMER + ".service"), "w") as f:
            f.write("[Unit]\nDescription=Disk Cleaner: weekly clean into the Recycle Bin\n\n"
                    "[Service]\nType=oneshot\n"
                    f"ExecStart={exe} clean --yes\n")
        with open(os.path.join(d, _TIMER + ".timer"), "w") as f:
            f.write("[Unit]\nDescription=Disk Cleaner: weekly clean\n\n"
                    "[Timer]\nOnCalendar=weekly\nPersistent=true\nRandomizedDelaySec=1h\n\n"
                    "[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=10)
        subprocess.run(["systemctl", "--user", "enable", "--now", _TIMER + ".timer"], check=True, timeout=10)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", _TIMER + ".timer"], check=False, timeout=10)
        for ext in (".timer", ".service"):
            try:
                os.unlink(os.path.join(d, _TIMER + ext))
            except OSError:
                pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=10)
