#!/usr/bin/env python3
r"""Disk Cleaner & Info -- a pure-stdlib tkinter GUI on top of ``diskkit``.

A single main window: a left sidebar of views (Clean, Large Files, Disk Usage,
System Monitor) and a main panel that swaps to the selected view.  Every
operation calls the tested core library (never re-implements the logic) and runs
on a background thread so the UI stays responsive; results are marshalled back
with ``self.after`` and shown in a clear inline result bar -- a summary line on
success, or the :class:`DiskKitError` message (never a raw traceback) on failure.

Design goals baked in here:
  * pure standard-library tkinter/ttk -- NO third-party GUI deps.  Dark mode is
    a ttk-style + palette swap.
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a message, returns 0) with no display.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the exe
    directory when ``sys.frozen`` is set -- never ``__file__``.
  * Destructive actions (Clean, delete large files, empty Recycle Bin) always
    confirm first, and default to the Recycle Bin (recoverable).

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter is imported lazily inside main()/build_app so that merely
# importing this module (packaging, headless CI) never fails.

APP_NAME = "Disk Cleaner & Info"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "Disk Cleaner & Info — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"

# (view_id, label) -- view_id maps to a _panel_<id> method.
VIEWS = [
    ("clean", "Clean"),
    ("large", "Large Files"),
    ("usage", "Disk Usage"),
    ("monitor", "System Monitor"),
]

VIEW_DESCRIPTIONS = {
    "clean": "Scan temporary files, browser caches and other junk, then reclaim "
             "the space. Deletions go to the Recycle Bin by default.",
    "large": "Find the biggest files under a folder and send the ones you pick "
             "to the Recycle Bin.",
    "usage": "See which sub-folders are using the most space under a folder.",
    "monitor": "Live CPU, memory, disk and network usage, plus the top processes.",
}

# ---- colour palettes (mirror the QuickOpen palette) -------------------------
PALETTES = {
    "light": {
        "bg": "#f5f7fa", "surface": "#ffffff", "text": "#141820",
        "muted": "#5b6472", "primary": "#2f5fe0", "primary_hi": "#2450c8",
        "entry": "#ffffff", "border": "#d5dae2", "sel": "#2f5fe0",
        "sel_fg": "#ffffff", "trough": "#e2e7ef", "ok": "#1f7a3d",
        "err": "#c0392b", "warn": "#b9770e", "bar": "#2f5fe0",
    },
    "dark": {
        "bg": "#0f1115", "surface": "#1a1e24", "text": "#f1f3f7",
        "muted": "#9aa4b2", "primary": "#5b86f7", "primary_hi": "#7098ff",
        "entry": "#1a1e24", "border": "#2a2f38", "sel": "#5b86f7",
        "sel_fg": "#0f1115", "trough": "#2a2f38", "ok": "#5bd68a",
        "err": "#ff6b5e", "warn": "#e2b04a", "bar": "#5b86f7",
    },
}

FONT = "Segoe UI"


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
# The app (built lazily; tkinter imported only inside build_app/main)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to a live tkinter import.

    Kept inside a function so this module imports cleanly without a display.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    from . import guiconfig
    from .errors import DiskKitError
    from .cleaners import (
        TARGETS, scan_targets, clean_targets, empty_recycle_bin,
    )
    from .largefiles import find_large_files, dir_sizes
    from . import sysinfo, monitor

    # ---- reusable folder picker row ------------------------------------
    class FolderRow(ttk.Frame):
        """A labelled 'folder + Browse' row that remembers recent picks."""

        def __init__(self, master, app, label, on_change=None):
            super().__init__(master, style="TFrame")
            self.app = app
            self.on_change = on_change
            self.var = tk.StringVar()
            ttk.Label(self, text=label, style="TLabel").pack(anchor="w")
            row = ttk.Frame(self, style="TFrame")
            row.pack(fill="x")
            self.entry = ttk.Entry(row, textvariable=self.var)
            self.entry.pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Browse…", command=self._browse).pack(
                side="left", padx=(6, 0))

        def _browse(self):
            initial = self.var.get() or os.path.expanduser("~")
            chosen = filedialog.askdirectory(initialdir=initial, mustexist=True)
            if chosen:
                self.set(chosen)

        def get(self):
            return self.var.get().strip()

        def set(self, value):
            self.var.set(value)
            if value:
                guiconfig.add_recent(value)
            if self.on_change:
                self.on_change(value)

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(WINDOW_TITLE)
            self.geometry("1080x700")
            self.minsize(900, 600)

            self.theme = guiconfig.get_theme()
            self._busy = False
            self._panels = {}          # view_id -> built frame (lazy)
            self._current = None
            self._current_id = None
            self._tracked = []         # (tk_widget, role) for manual re-theming
            self._img_refs = []        # keep PhotoImage refs alive

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
            self._build_layout()
            self._apply_theme()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, self._select_first_view)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("disk-cleaner.ico")
                if ico:
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("disk-cleaner.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- theming
        def track(self, widget, role):
            self._tracked.append((widget, role))

        def _pal(self):
            return PALETTES[self.theme]

        def _apply_theme(self):
            p = self._pal()
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            self.configure(bg=p["bg"])
            style.configure(".", background=p["bg"], foreground=p["text"],
                            fieldbackground=p["entry"], bordercolor=p["border"],
                            font=(FONT, 10))
            style.configure("TFrame", background=p["bg"])
            style.configure("Sidebar.TFrame", background=p["surface"])
            style.configure("Card.TFrame", background=p["surface"])
            style.configure("TLabel", background=p["bg"], foreground=p["text"])
            style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
            style.configure("Header.TLabel", background=p["bg"], foreground=p["text"],
                            font=(FONT, 15, "bold"))
            style.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"],
                            font=(FONT, 10))
            style.configure("Big.TLabel", background=p["bg"], foreground=p["text"],
                            font=(FONT, 20, "bold"))
            style.configure("Brand.TLabel", background=p["surface"],
                            foreground=p["text"], font=(FONT, 12, "bold"))
            style.configure("Ok.TLabel", background=p["bg"], foreground=p["ok"])
            style.configure("Err.TLabel", background=p["bg"], foreground=p["err"])
            style.configure("Status.TLabel", background=p["surface"],
                            foreground=p["muted"])
            style.configure("TButton", background=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], focuscolor=p["surface"],
                            padding=(10, 5))
            style.map("TButton",
                      background=[("active", p["trough"]), ("disabled", p["bg"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Accent.TButton", background=p["primary"],
                            foreground="#ffffff", padding=(12, 6))
            style.map("Accent.TButton",
                      background=[("active", p["primary_hi"]),
                                  ("disabled", p["border"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Danger.TButton", background=p["err"],
                            foreground="#ffffff", padding=(12, 6))
            style.map("Danger.TButton",
                      background=[("active", p["err"]), ("disabled", p["border"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Toggle.TButton", background=p["surface"],
                            foreground=p["text"], padding=(8, 4))
            for name in ("TEntry", "TSpinbox"):
                style.configure(name, fieldbackground=p["entry"], foreground=p["text"],
                                insertcolor=p["text"], bordercolor=p["border"])
            style.configure("TCombobox", fieldbackground=p["entry"],
                            foreground=p["text"], background=p["surface"],
                            arrowcolor=p["text"])
            style.map("TCombobox", fieldbackground=[("readonly", p["entry"])],
                      foreground=[("readonly", p["text"])])
            style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
            style.map("TCheckbutton", background=[("active", p["bg"])])
            style.configure("Card.TCheckbutton", background=p["surface"],
                            foreground=p["text"])
            style.map("Card.TCheckbutton", background=[("active", p["surface"])])
            style.configure("TLabelframe", background=p["bg"], foreground=p["text"],
                            bordercolor=p["border"])
            style.configure("TLabelframe.Label", background=p["bg"],
                            foreground=p["muted"])
            style.configure("Treeview", background=p["surface"],
                            fieldbackground=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], rowheight=24)
            style.map("Treeview", background=[("selected", p["primary"])],
                      foreground=[("selected", p["sel_fg"])])
            style.configure("Treeview.Heading", background=p["surface"],
                            foreground=p["muted"], font=(FONT, 9, "bold"))
            style.configure("Sidebar.Treeview", background=p["surface"],
                            fieldbackground=p["surface"], rowheight=30,
                            font=(FONT, 11))
            style.configure("TProgressbar", background=p["bar"],
                            troughcolor=p["trough"], bordercolor=p["border"])
            style.configure("Horizontal.TProgressbar", background=p["bar"],
                            troughcolor=p["trough"], bordercolor=p["border"])
            style.configure("TScrollbar", background=p["surface"],
                            troughcolor=p["bg"], bordercolor=p["border"],
                            arrowcolor=p["text"])
            style.configure("TSeparator", background=p["border"])

            for widget, role in list(self._tracked):
                try:
                    if role == "listbox":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"], borderwidth=0)
                    elif role == "text":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         insertbackground=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"], borderwidth=0)
                    elif role == "canvas":
                        widget.configure(bg=p["surface"], highlightthickness=1,
                                         highlightbackground=p["border"])
                    elif role == "surface":
                        widget.configure(background=p["surface"])
                except Exception:
                    pass

        def toggle_theme(self):
            self.theme = "dark" if self.theme == "light" else "light"
            guiconfig.set_theme(self.theme)
            self._apply_theme()
            self._theme_btn.configure(
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")

        # ---- menu
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)
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

        # ---- layout
        def _build_layout(self):
            top = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 8))
            top.pack(fill="x", side="top")
            ttk.Label(top, text="Disk Cleaner & Info", style="Brand.TLabel").pack(
                side="left")
            ttk.Label(top, style="Status.TLabel",
                      text="  offline · open source · by QuickOpen").pack(side="left")
            self._theme_btn = ttk.Button(
                top, style="Toggle.TButton", command=self.toggle_theme,
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            self._theme_btn.pack(side="right")

            body = ttk.Frame(self, style="TFrame")
            body.pack(fill="both", expand=True)

            side = ttk.Frame(body, style="Sidebar.TFrame", width=200)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            self.nav = ttk.Treeview(side, show="tree", selectmode="browse",
                                    style="Sidebar.Treeview")
            self.nav.pack(fill="both", expand=True, padx=6, pady=6)
            self._nav_ids = {}
            for vid, label in VIEWS:
                iid = self.nav.insert("", "end", text="  " + label)
                self._nav_ids[iid] = vid
            self.nav.bind("<<TreeviewSelect>>", self._on_nav_select)

            main = ttk.Frame(body, style="TFrame", padding=(16, 12))
            main.pack(side="left", fill="both", expand=True)

            head = ttk.Frame(main, style="TFrame")
            head.pack(fill="x")
            self.title_lbl = ttk.Label(head, text="Welcome", style="Header.TLabel")
            self.title_lbl.pack(anchor="w")
            self.desc_lbl = ttk.Label(head, text="", style="Sub.TLabel",
                                      wraplength=760, justify="left")
            self.desc_lbl.pack(anchor="w", pady=(2, 8))
            ttk.Separator(main).pack(fill="x")

            self.container = ttk.Frame(main, style="TFrame")
            self.container.pack(fill="both", expand=True, pady=(10, 8))

            bar = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 6))
            bar.pack(fill="x", side="bottom")
            self.status_lbl = ttk.Label(bar, text="Ready", style="Status.TLabel",
                                        width=14, anchor="w")
            self.status_lbl.pack(side="left")
            self.progress = ttk.Progressbar(bar, mode="indeterminate", length=120)
            self.result_lbl = ttk.Label(bar, text="", style="Status.TLabel",
                                        anchor="w", wraplength=720, justify="left")
            self.result_lbl.pack(side="left", fill="x", expand=True, padx=8)

        def _select_first_view(self):
            for iid in self._nav_ids:
                self.nav.selection_set(iid)
                self.nav.see(iid)
                break

        def _on_nav_select(self, _e=None):
            sel = self.nav.selection()
            if not sel:
                return
            vid = self._nav_ids.get(sel[0])
            if vid:
                self._show_view(vid)

        def _show_view(self, view_id):
            # Leaving the monitor stops its background sampling.
            if self._current_id == "monitor" and view_id != "monitor":
                self._stop_monitor()
            if self._current is not None:
                self._current.pack_forget()
            panel = self._panels.get(view_id)
            if panel is None:
                panel = ttk.Frame(self.container, style="TFrame")
                builder = getattr(self, "_panel_" + view_id, None)
                if builder:
                    builder(panel)
                else:
                    ttk.Label(panel, text="Not implemented.").pack()
                self._panels[view_id] = panel
                self._apply_theme()
            panel.pack(fill="both", expand=True)
            self._current = panel
            self._current_id = view_id
            for _vid, label in VIEWS:
                if _vid == view_id:
                    self.title_lbl.configure(text=label)
            self.desc_lbl.configure(text=VIEW_DESCRIPTIONS.get(view_id, ""))
            self._clear_result()

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
            self._clear_result(keep_status=True)
            try:
                self.progress.pack(side="left", padx=(4, 0))
                self.progress.start(12)
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
                    self._set_status("error", kind="err")
                    self._show_error(err)
                    return
                self._set_status("done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- result bar helpers
        def _set_status(self, text, kind="idle"):
            p = self._pal()
            color = {"working": p["primary"], "ok": p["ok"], "err": p["err"]}.get(
                kind, p["muted"])
            self.status_lbl.configure(text=text, foreground=color)

        def _clear_result(self, keep_status=False):
            self.result_lbl.configure(text="")
            if not keep_status:
                self._set_status("Ready")

        def _show_error(self, message):
            self.result_lbl.configure(text="✕ " + message,
                                      foreground=self._pal()["err"])

        def report_success(self, message):
            self.result_lbl.configure(text="✓ " + message,
                                      foreground=self._pal()["ok"])
            self._set_status("done", kind="ok")

        # ===============================================================
        # Clean view
        # ===============================================================
        def _panel_clean(self, parent):
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            self._clean_scan_btn = ttk.Button(
                top, text="Scan for junk", style="Accent.TButton",
                command=self._clean_do_scan)
            self._clean_scan_btn.pack(side="left")
            self._clean_perm = tk.BooleanVar(value=False)
            ttk.Checkbutton(top, text="Delete permanently (skip Recycle Bin)",
                            variable=self._clean_perm,
                            style="TCheckbutton").pack(side="left", padx=12)

            mid = ttk.Frame(parent, style="Card.TFrame")
            mid.pack(fill="both", expand=True, pady=10)
            # Scrollable checklist of targets.
            canvas = tk.Canvas(mid, highlightthickness=0, borderwidth=0)
            sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
            self._clean_list = ttk.Frame(canvas, style="Card.TFrame")
            self._clean_list.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=self._clean_list, anchor="nw")
            canvas.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            self.track(canvas, "canvas")
            self._clean_canvas = canvas

            ttk.Label(self._clean_list, style="Sub.TLabel",
                      text="Click “Scan for junk” to see what can be cleaned.").pack(
                anchor="w", padx=10, pady=10)

            bottom = ttk.Frame(parent, style="TFrame")
            bottom.pack(fill="x")
            self._clean_total = ttk.Label(bottom, text="Reclaimable: —",
                                          style="Big.TLabel")
            self._clean_total.pack(side="left")
            self._clean_btn = ttk.Button(
                bottom, text="Clean selected", style="Danger.TButton",
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
                    row = ttk.Frame(self._clean_list, style="Card.TFrame")
                    row.pack(fill="x", padx=8, pady=3)
                    cb = ttk.Checkbutton(
                        row, variable=var, style="Card.TCheckbutton",
                        text=f"{target.name}  —  {human_size(nbytes)} "
                             f"({files} files)")
                    cb.pack(side="left", anchor="w")
                    if not files:
                        try:
                            cb.state(["disabled"])
                        except Exception:
                            pass
                    ttk.Label(row, text=target.description, style="Sub.TLabel",
                              wraplength=560, justify="left").pack(
                        side="left", padx=10)
                self._clean_total.configure(
                    text=f"Reclaimable: {human_size(total)}")
                try:
                    self._clean_btn.state(
                        ["!disabled"] if total else ["disabled"])
                except Exception:
                    pass
                self._apply_theme()
                self.report_success(
                    f"Scan complete — {human_size(total)} reclaimable.")

            self._bg(work, done, button=self._clean_scan_btn, busy="Scanning…")

        def _clean_add_special(self, target, info):
            row = ttk.Frame(self._clean_list, style="Card.TFrame")
            row.pack(fill="x", padx=8, pady=3)
            ttk.Label(row, text=f"{target.name} — {target.description}",
                      style="Sub.TLabel", wraplength=560, justify="left").pack(
                side="left", anchor="w")
            btn = ttk.Button(row, text="Empty Recycle Bin…",
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
        # Large Files view
        # ===============================================================
        def _panel_large(self, parent):
            self._large_folder = FolderRow(parent, self, "Folder to scan")
            self._large_folder.pack(fill="x", pady=4)
            opts = ttk.Frame(parent, style="TFrame")
            opts.pack(fill="x", pady=4)
            ttk.Label(opts, text="Minimum size:").pack(side="left")
            self._large_min = ttk.Combobox(
                opts, width=8, values=["1MB", "10MB", "50MB", "100MB", "500MB", "1GB"])
            self._large_min.set("10MB")
            self._large_min.pack(side="left", padx=(4, 12))
            ttk.Label(opts, text="Max results:").pack(side="left")
            self._large_limit = ttk.Spinbox(opts, from_=10, to=1000, width=6)
            self._large_limit.set(100)
            self._large_limit.pack(side="left", padx=(4, 12))
            self._large_find_btn = ttk.Button(
                opts, text="Find large files", style="Accent.TButton",
                command=self._large_do_find)
            self._large_find_btn.pack(side="left")

            table = ttk.Frame(parent, style="TFrame")
            table.pack(fill="both", expand=True, pady=8)
            cols = ("size", "path")
            self._large_tree = ttk.Treeview(
                table, columns=cols, show="headings", selectmode="extended")
            self._large_tree.heading(
                "size", text="Size ▾", command=lambda: self._large_sort_by("bytes"))
            self._large_tree.heading(
                "path", text="Path", command=lambda: self._large_sort_by("path"))
            self._large_tree.column("size", width=110, anchor="e", stretch=False)
            self._large_tree.column("path", width=640, anchor="w")
            sb = ttk.Scrollbar(table, orient="vertical",
                               command=self._large_tree.yview)
            self._large_tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._large_tree.pack(side="left", fill="both", expand=True)

            actions = ttk.Frame(parent, style="TFrame")
            actions.pack(fill="x")
            self._large_del_btn = ttk.Button(
                actions, text="Send selected to Recycle Bin",
                style="Danger.TButton", command=self._large_delete)
            self._large_del_btn.pack(side="left")
            ttk.Button(actions, text="Open containing folder",
                       command=self._large_reveal).pack(side="left", padx=8)
            self._large_perm = tk.BooleanVar(value=False)
            ttk.Checkbutton(actions, text="Delete permanently",
                            variable=self._large_perm,
                            style="TCheckbutton").pack(side="left", padx=8)

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
            tree.heading("size", text="Size " + ("▾" if desc else "▴")
                         if key == "bytes" else "Size")
            tree.heading("path", text="Path " + ("▾" if desc else "▴")
                         if key == "path" else "Path")
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
        # Disk Usage view
        # ===============================================================
        def _panel_usage(self, parent):
            self._usage_folder = FolderRow(parent, self, "Folder to analyse")
            self._usage_folder.pack(fill="x", pady=4)
            opts = ttk.Frame(parent, style="TFrame")
            opts.pack(fill="x", pady=4)
            ttk.Label(opts, text="Depth:").pack(side="left")
            self._usage_depth = ttk.Spinbox(opts, from_=1, to=6, width=5)
            self._usage_depth.set(2)
            self._usage_depth.pack(side="left", padx=(4, 12))
            self._usage_btn = ttk.Button(
                opts, text="Analyse usage", style="Accent.TButton",
                command=self._usage_do)
            self._usage_btn.pack(side="left")

            table = ttk.Frame(parent, style="TFrame")
            table.pack(fill="both", expand=True, pady=8)
            self._usage_tree = ttk.Treeview(
                table, columns=("size", "pct", "files"), show="tree headings")
            self._usage_tree.heading("#0", text="Folder")
            self._usage_tree.heading("size", text="Size")
            self._usage_tree.heading("pct", text="Share")
            self._usage_tree.heading("files", text="Files")
            self._usage_tree.column("#0", width=440, anchor="w")
            self._usage_tree.column("size", width=110, anchor="e", stretch=False)
            self._usage_tree.column("pct", width=140, anchor="w", stretch=False)
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
        # System Monitor view
        # ===============================================================
        def _panel_monitor(self, parent):
            ctl = ttk.Frame(parent, style="TFrame")
            ctl.pack(fill="x")
            self._mon_btn = ttk.Button(
                ctl, text="▶ Start monitor", style="Accent.TButton",
                command=self._toggle_monitor)
            self._mon_btn.pack(side="left")
            ttk.Label(ctl, text="  updates live on a background thread",
                      style="Sub.TLabel").pack(side="left")

            grid = ttk.Frame(parent, style="TFrame")
            grid.pack(fill="both", expand=True, pady=8)

            # left column: CPU + memory dials
            left = ttk.Labelframe(grid, text="CPU / Memory", padding=10)
            left.pack(side="left", fill="both", expand=True, padx=(0, 6))
            self._mon_cpu_lbl = ttk.Label(left, text="CPU: —", style="TLabel")
            self._mon_cpu_lbl.pack(anchor="w")
            self._mon_cpu_bar = ttk.Progressbar(left, maximum=100, length=260)
            self._mon_cpu_bar.pack(fill="x", pady=(0, 8))
            self._mon_cores = ttk.Frame(left, style="TFrame")
            self._mon_cores.pack(fill="x")
            self._mon_mem_lbl = ttk.Label(left, text="Memory: —", style="TLabel")
            self._mon_mem_lbl.pack(anchor="w", pady=(8, 0))
            self._mon_mem_bar = ttk.Progressbar(left, maximum=100, length=260)
            self._mon_mem_bar.pack(fill="x")
            self._mon_swap_lbl = ttk.Label(left, text="Swap: —", style="TLabel")
            self._mon_swap_lbl.pack(anchor="w", pady=(8, 0))
            self._mon_swap_bar = ttk.Progressbar(left, maximum=100, length=260)
            self._mon_swap_bar.pack(fill="x")
            self._mon_net_lbl = ttk.Label(left, text="Network: —", style="Sub.TLabel")
            self._mon_net_lbl.pack(anchor="w", pady=(8, 0))
            self._mon_bat_lbl = ttk.Label(left, text="", style="Sub.TLabel")
            self._mon_bat_lbl.pack(anchor="w")

            # right column: disks + processes
            right = ttk.Frame(grid, style="TFrame")
            right.pack(side="left", fill="both", expand=True, padx=(6, 0))
            dframe = ttk.Labelframe(right, text="Disks", padding=6)
            dframe.pack(fill="x")
            self._mon_disks = ttk.Treeview(
                dframe, columns=("used", "pct"), show="tree headings", height=5)
            self._mon_disks.heading("#0", text="Mount")
            self._mon_disks.heading("used", text="Used / Total")
            self._mon_disks.heading("pct", text="%")
            self._mon_disks.column("#0", width=140)
            self._mon_disks.column("used", width=180, anchor="e")
            self._mon_disks.column("pct", width=60, anchor="e")
            self._mon_disks.pack(fill="x")

            pframe = ttk.Labelframe(right, text="Top processes by memory", padding=6)
            pframe.pack(fill="both", expand=True, pady=(8, 0))
            self._mon_proc = ttk.Treeview(
                pframe, columns=("pid", "mem", "cpu"), show="tree headings")
            self._mon_proc.heading("#0", text="Process")
            self._mon_proc.heading("pid", text="PID")
            self._mon_proc.heading("mem", text="Memory")
            self._mon_proc.heading("cpu", text="CPU %")
            self._mon_proc.column("#0", width=180)
            self._mon_proc.column("pid", width=70, anchor="e")
            self._mon_proc.column("mem", width=110, anchor="e")
            self._mon_proc.column("cpu", width=70, anchor="e")
            sb = ttk.Scrollbar(pframe, orient="vertical",
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
            self._set_status("monitoring", kind="working")

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
                self._mon_cpu_bar.configure(value=cpu["percent"])
                cores = cpu.get("per_core") or []
                if len(self._core_bars) != len(cores):
                    for w in self._mon_cores.winfo_children():
                        w.destroy()
                    self._core_bars = []
                    for i in range(len(cores)):
                        b = ttk.Progressbar(self._mon_cores, maximum=100, length=120)
                        b.grid(row=i // 2, column=i % 2, padx=3, pady=2, sticky="we")
                        self._core_bars.append(b)
                for b, val in zip(self._core_bars, cores):
                    b.configure(value=val)

                vm = snap["memory"].get("virtual") or {}
                self._mon_mem_lbl.configure(
                    text=f"Memory: {vm.get('percent', 0):.0f}%  "
                         f"({human_size(vm.get('used', 0))} / "
                         f"{human_size(vm.get('total', 0))})")
                self._mon_mem_bar.configure(value=vm.get("percent", 0))
                sw = snap["memory"].get("swap") or {}
                self._mon_swap_lbl.configure(
                    text=f"Swap: {sw.get('percent', 0):.0f}%  "
                         f"({human_size(sw.get('used', 0))} / "
                         f"{human_size(sw.get('total', 0))})")
                self._mon_swap_bar.configure(value=sw.get("percent", 0))

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
    With no display (e.g. a server), it prints a friendly note and returns 0
    instead of raising.
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
