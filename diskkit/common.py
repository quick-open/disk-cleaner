"""Shared low-level helpers used across diskkit modules.

Everything here is pure stdlib and safe to import on any platform.  The disk
walkers are deliberately defensive: a single unreadable file or a permission
error on one directory must never abort a whole scan.
"""

from __future__ import annotations

import os

from .errors import DiskKitError


def human_size(num_bytes):
    """Human-readable byte size, e.g. ``1.1MB``.

    Mirrors pdftoolkit's helper so the two apps report sizes identically.
    """
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


def expand(path, env=None):
    r"""Expand environment variables and ``~`` in *path*.

    Handles both Windows (``%TEMP%``) and POSIX (``$TMPDIR``) styles.  When
    *env* is supplied it is used in place of ``os.environ`` -- the hook the
    tests use to point Windows-style roots at a temp directory on Linux.
    """
    if path is None:
        return None
    text = str(path)
    source = os.environ if env is None else env

    # %VAR% (Windows style) -- os.path.expandvars only does this on Windows,
    # so we expand it ourselves for cross-platform testability.
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%":
            end = text.find("%", i + 1)
            if end != -1:
                name = text[i + 1:end]
                if name == "":            # literal "%%" -> "%"
                    out.append("%")
                    i = end + 1
                    continue
                if name in source:
                    out.append(source[name])
                    i = end + 1
                    continue
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    text = "".join(out)

    # $VAR / ${VAR} (POSIX style) via a private-environ expandvars.
    if "$" in text:
        saved = os.environ
        try:
            if env is not None:
                os.environ = env  # type: ignore[assignment]
            text = os.path.expandvars(text)
        finally:
            os.environ = saved  # type: ignore[assignment]
    return os.path.expanduser(text)


def iter_files(root, on_error=None):
    """Yield ``(path, size_bytes)`` for every regular file under *root*.

    Uses :func:`os.scandir` for speed, follows no symlinks, and silently skips
    entries it cannot stat (permission errors, races, broken links).  *on_error*
    -- if given -- is called with the offending path for observability.
    """
    if not root or not os.path.isdir(root):
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                size = 0
                            yield entry.path, size
                    except OSError:
                        if on_error:
                            on_error(entry.path)
                        continue
        except OSError:
            if on_error:
                on_error(current)
            continue


def dir_stats(root):
    """Return ``(file_count, total_bytes)`` for everything under *root*.

    Never raises on a missing root -- returns ``(0, 0)`` -- so scans over
    optional cache locations degrade quietly.
    """
    count = 0
    total = 0
    for _path, size in iter_files(root):
        count += 1
        total += size
    return count, total


__all__ = [
    "DiskKitError",
    "human_size",
    "expand",
    "iter_files",
    "dir_stats",
]
