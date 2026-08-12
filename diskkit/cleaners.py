r"""Cleanup targets, scanning, and (safe) cleaning.

Targets are declared as *data* -- a name, a human description, and a list of
path specs written with Windows environment variables (``%TEMP%``,
``%LOCALAPPDATA%\Temp``, browser cache folders, ...).  A spec is either a
directory (whose direct children are the deletable units) or a glob such as
``%TEMP%\*.tmp``.

Two operations sit on top of that data:

* :func:`scan_targets` -- **read-only**.  Resolves each target's specs and
  reports how many files and how many bytes could be reclaimed.  It never
  deletes anything.
* :func:`clean_targets` -- deletes the resolved entries, sending them to the
  Recycle Bin by default (``send2trash``) and skipping anything locked or
  in use.  Returns the number of bytes freed.

Both accept an optional ``env`` (an environment mapping) and an optional
``roots`` override (``{target_id: [directory, ...]}``).  Those two hooks make
the whole Windows-shaped target table testable headless on Linux: point a
target at a temp tree and exercise the real scan/clean code paths.

Emptying the Recycle Bin itself is a separate, explicit call
(:func:`empty_recycle_bin`) -- it is never triggered by ``clean_targets``.
"""

from __future__ import annotations

import glob as _glob
import os
import shutil

from .common import dir_stats, expand
from .errors import DiskKitError

_GLOB_CHARS = set("*?[")


class Target:
    """A named cleanup location, declared as data.

    ``special`` marks entries that cannot be expressed as file paths (the
    Recycle Bin).  Such targets are skipped by scan/clean and handled by a
    dedicated function instead.
    """

    __slots__ = ("id", "name", "description", "patterns", "special")

    def __init__(self, id, name, description, patterns=(), special=None):
        self.id = id
        self.name = name
        self.description = description
        self.patterns = list(patterns)
        self.special = special

    def __repr__(self):  # pragma: no cover - debug aid
        return f"<Target {self.id!r} ({len(self.patterns)} patterns)>"


# ---------------------------------------------------------------------------
# The target table.  Windows-first paths; harmless (they simply resolve to
# nothing) on other platforms.
# ---------------------------------------------------------------------------
TARGETS = [
    Target(
        "user_temp", "User temp files",
        "Your per-user %TEMP% folder — installers, scratch files and app leftovers.",
        [r"%TEMP%", r"%TMP%", r"%LOCALAPPDATA%\Temp"],
    ),
    Target(
        "windows_temp", "Windows temp files",
        "The system-wide C:\\Windows\\Temp scratch folder.",
        [r"%SystemRoot%\Temp", r"%WINDIR%\Temp", r"C:\Windows\Temp"],
    ),
    Target(
        "chrome_cache", "Chrome cache",
        "Google Chrome's on-disk web cache (safe to clear; pages just re-download).",
        [
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\GPUCache",
        ],
    ),
    Target(
        "edge_cache", "Edge cache",
        "Microsoft Edge's on-disk web cache.",
        [
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Code Cache",
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\GPUCache",
        ],
    ),
    Target(
        "firefox_cache", "Firefox cache",
        "Mozilla Firefox's disk cache across all profiles.",
        [
            r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2",
            r"%APPDATA%\Mozilla\Firefox\Profiles\*\cache2",
        ],
    ),
    Target(
        "thumbnail_cache", "Thumbnail cache",
        "Explorer's thumbnail database (thumbcache_*.db) — rebuilt on demand.",
        [
            r"%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache_*.db",
            r"%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_*.db",
        ],
    ),
    Target(
        "windows_update", "Windows Update cache",
        "Downloaded Windows Update packages (SoftwareDistribution\\Download).",
        [r"%SystemRoot%\SoftwareDistribution\Download", r"%WINDIR%\SoftwareDistribution\Download"],
    ),
    Target(
        "crash_dumps", "Crash dumps & error reports",
        "Application crash dumps and Windows Error Reporting queues.",
        [
            r"%LOCALAPPDATA%\CrashDumps",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportQueue",
            r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportArchive",
        ],
    ),
    Target(
        "delivery_optimization", "Delivery Optimization files",
        "Peer-to-peer update download cache.",
        [r"%SystemRoot%\SoftwareDistribution\DeliveryOptimization"],
    ),
    Target(
        "recycle_bin", "Recycle Bin",
        "Files you have already deleted, still occupying disk space.",
        [], special="recycle_bin",
    ),
]

TARGETS_BY_ID = {t.id: t for t in TARGETS}


def target_ids():
    """All known target ids, in table order."""
    return [t.id for t in TARGETS]


def _selected_targets(selected):
    """Resolve *selected* (ids, Target objects, or ``None`` for all) to Targets."""
    if selected is None:
        return list(TARGETS)
    out = []
    for item in selected:
        if isinstance(item, Target):
            out.append(item)
            continue
        t = TARGETS_BY_ID.get(item)
        if t is None:
            raise DiskKitError(f"unknown cleanup target: {item!r}")
        out.append(t)
    return out


def _resolve_entries(target, env=None, roots=None):
    """Return the concrete, existing paths a target would scan/clean.

    Each returned path is one deletable unit -- a file, or a directory whose
    whole subtree counts.  Cleaning empties a target's directories rather than
    removing the cache folder itself, so we descend one level into resolved
    directories and return their children.
    """
    if target.special:
        return []

    # Explicit override wins: treat each given directory as a target root.
    if roots and target.id in roots:
        base_dirs = roots[target.id]
        specs = list(base_dirs)
        override = True
    else:
        specs = [expand(p, env) for p in target.patterns]
        override = False

    entries = []
    seen = set()

    def _add(path):
        # Dedupe by normalised absolute path so overlapping specs (e.g. %TEMP%
        # and %TMP%, which are usually the same folder on Windows) are neither
        # double-counted by a scan nor double-deleted by a clean.
        try:
            key = os.path.normcase(os.path.abspath(path))
        except Exception:
            key = path
        if key in seen:
            return
        seen.add(key)
        entries.append(path)

    for spec in specs:
        if not spec:
            continue
        if not override and any(c in spec for c in _GLOB_CHARS):
            for match in _glob.glob(spec):
                _add(match)
            continue
        if os.path.isdir(spec) and not os.path.islink(spec):
            try:
                for name in os.listdir(spec):
                    _add(os.path.join(spec, name))
            except OSError:
                continue
        elif os.path.exists(spec):
            _add(spec)
    return entries


def _entry_size(path):
    """Bytes occupied by *path* (recursing into directories)."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return dir_stats(path)[1]
        return os.path.getsize(path)
    except OSError:
        return 0


def _entry_count(path):
    """Number of files represented by *path* (1 for a file, N for a tree)."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return dir_stats(path)[0]
        return 1
    except OSError:
        return 0


def scan_targets(selected=None, env=None, roots=None):
    """Scan (never delete) the *selected* targets and size what's reclaimable.

    Returns ``{target_id: {"name", "description", "files", "bytes", "special"}}``
    for every selected target, so a caller can present a checklist with sizes.
    ``selected`` may be ``None`` (all targets), a list of ids, or Target objects.
    """
    result = {}
    for target in _selected_targets(selected):
        files = 0
        total = 0
        if not target.special:
            for path in _resolve_entries(target, env=env, roots=roots):
                files += _entry_count(path)
                total += _entry_size(path)
        result[target.id] = {
            "id": target.id,
            "name": target.name,
            "description": target.description,
            "files": files,
            "bytes": total,
            "special": target.special,
        }
    return result


def clean_targets(selected=None, to_trash=True, env=None, roots=None):
    """Delete the *selected* targets' entries and return the bytes freed.

    By default entries go to the Recycle Bin via ``send2trash`` (recoverable);
    pass ``to_trash=False`` to delete permanently.  Anything locked, in use, or
    otherwise un-deletable is skipped -- it simply isn't counted toward the
    freed total.  Special targets (the Recycle Bin) are ignored here; empty it
    with :func:`empty_recycle_bin`.
    """
    freed = 0
    for target in _selected_targets(selected):
        if target.special:
            continue
        for path in _resolve_entries(target, env=env, roots=roots):
            size = _entry_size(path)
            try:
                _delete_path(path, to_trash=to_trash)
            except Exception:
                # Locked / in use / permission denied -> skip, don't count.
                continue
            freed += size
    return freed


def _delete_path(path, to_trash=True):
    """Delete a single path, to the Recycle Bin or permanently.

    Raises on failure (the caller decides whether to skip).  ``send2trash`` is
    imported lazily so importing this module never depends on it, and so the
    permanent path stays usable on headless hosts where trashing may no-op.
    """
    if to_trash:
        try:
            from send2trash import send2trash as _s2t
        except Exception as exc:  # pragma: no cover - dependency missing
            raise DiskKitError(f"send2trash unavailable: {exc}") from exc
        _s2t(path)
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def empty_recycle_bin(confirm=True):
    """Permanently empty the Windows Recycle Bin (explicit, destructive).

    This is deliberately separate from :func:`clean_targets`: emptying the
    Recycle Bin cannot be undone.  On non-Windows hosts there is no Recycle Bin,
    so this raises :class:`DiskKitError` rather than pretending to succeed.
    ``confirm`` must be truthy -- a small guard against accidental calls.
    """
    if not confirm:
        raise DiskKitError("emptying the Recycle Bin requires confirm=True")
    if os.name != "nt":
        raise DiskKitError(
            "emptying the Recycle Bin is only supported on Windows")
    try:  # pragma: no cover - Windows-only path
        import ctypes
        SHERB_NOCONFIRMATION = 0x00000001
        SHERB_NOPROGRESSUI = 0x00000002
        SHERB_NOSOUND = 0x00000004
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        res = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
        # S_OK (0) or "already empty" (0x8000FE2D) both mean success.
        if res not in (0, 0x8000FE2D):
            raise DiskKitError(f"SHEmptyRecycleBinW failed (0x{res & 0xFFFFFFFF:08X})")
    except DiskKitError:
        raise
    except Exception as exc:  # pragma: no cover - Windows-only path
        raise DiskKitError(f"could not empty the Recycle Bin: {exc}") from exc
    return True


__all__ = [
    "Target",
    "TARGETS",
    "TARGETS_BY_ID",
    "target_ids",
    "scan_targets",
    "clean_targets",
    "empty_recycle_bin",
]
