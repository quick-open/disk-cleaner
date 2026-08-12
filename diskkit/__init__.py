"""diskkit -- a permissively-licensed disk cleanup & system info library.

Public API, grouped by area:

* **Cleaning** -- :data:`TARGETS`, :func:`scan_targets` (read-only),
  :func:`clean_targets` (deletes to the Recycle Bin by default), and
  :func:`empty_recycle_bin` (explicit).
* **Disk usage** -- :func:`find_large_files`, :func:`dir_sizes`.
* **System info** -- :func:`snapshot`, :func:`disks`, and the live
  :func:`sample` generator used by the monitor view.

Every function raises :class:`DiskKitError` (and only that) on recoverable
failure.  Importing this package has no side effects; the tkinter GUI lives in
:mod:`diskkit.gui` and is built lazily.  See ``packaging`` / README for details.
"""

from __future__ import annotations

from .errors import DiskKitError
from .common import human_size, expand
from .cleaners import (
    TARGETS,
    TARGETS_BY_ID,
    Target,
    target_ids,
    scan_targets,
    clean_targets,
    empty_recycle_bin,
)
from .largefiles import find_large_files, dir_sizes
from .sysinfo import (
    snapshot,
    cpu_info,
    memory_info,
    disks,
    net_info,
    top_processes,
    battery,
)
from .monitor import sample, sample_for

__version__ = "1.0.0"

__all__ = [
    "DiskKitError",
    "human_size",
    "expand",
    "TARGETS",
    "TARGETS_BY_ID",
    "Target",
    "target_ids",
    "scan_targets",
    "clean_targets",
    "empty_recycle_bin",
    "find_large_files",
    "dir_sizes",
    "snapshot",
    "cpu_info",
    "memory_info",
    "disks",
    "net_info",
    "top_processes",
    "battery",
    "sample",
    "sample_for",
]
