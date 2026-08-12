"""Find the biggest files and the heaviest folders under a directory.

Both helpers use :func:`os.scandir` walks (via :mod:`diskkit.common`) and treat
permission errors as "skip and keep going" rather than failures -- a usage scan
of ``C:\\`` will always hit folders it cannot read.
"""

from __future__ import annotations

import os

from .common import iter_files
from .errors import DiskKitError


def find_large_files(root, min_bytes=0, limit=100):
    """Return the largest files under *root*, biggest first.

    Only files at least *min_bytes* in size are considered; at most *limit* are
    returned.  Each item is ``{"path", "bytes", "name"}``.  Symlinks are not
    followed and unreadable entries are silently skipped.
    """
    if not root or not os.path.isdir(root):
        raise DiskKitError(f"not a directory: {root!r}")
    if min_bytes < 0:
        raise DiskKitError("min_bytes must be >= 0")

    found = []
    for path, size in iter_files(root):
        if size >= min_bytes:
            found.append((size, path))
    found.sort(key=lambda t: t[0], reverse=True)
    if limit is not None and limit >= 0:
        found = found[:limit]
    return [
        {"path": path, "bytes": size, "name": os.path.basename(path)}
        for size, path in found
    ]


def dir_sizes(root, depth=1):
    """Return a folder-size tree rooted at *root*, for a usage view.

    Every node is a dict::

        {"path", "name", "bytes", "files", "children": [node, ...]}

    ``bytes``/``files`` are recursive totals for the whole subtree, so a parent
    always sums its descendants.  *depth* limits how many levels of ``children``
    are materialised (``depth=0`` -> totals only, no children); the totals stay
    correct regardless of depth.  Children are sorted largest-first.
    """
    if not root or not os.path.isdir(root):
        raise DiskKitError(f"not a directory: {root!r}")
    if depth < 0:
        raise DiskKitError("depth must be >= 0")
    return _build_node(os.path.abspath(root), depth)


def _build_node(path, depth):
    """Recursively size *path*, expanding children while *depth* > 0."""
    total_bytes = 0
    total_files = 0
    children = []

    subdirs = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            total_bytes += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                        total_files += 1
                except OSError:
                    continue
    except OSError:
        # Unreadable directory -> report it as an empty node rather than fail.
        pass

    for sub in subdirs:
        if depth > 0:
            node = _build_node(sub, depth - 1)
        else:
            # Beyond the requested depth we still need the subtree's totals.
            b, f = _subtree_totals(sub)
            node = {
                "path": sub, "name": os.path.basename(sub) or sub,
                "bytes": b, "files": f, "children": [],
            }
        total_bytes += node["bytes"]
        total_files += node["files"]
        if depth > 0:
            children.append(node)

    children.sort(key=lambda n: n["bytes"], reverse=True)
    return {
        "path": path,
        "name": os.path.basename(path) or path,
        "bytes": total_bytes,
        "files": total_files,
        "children": children,
    }


def _subtree_totals(root):
    """``(bytes, files)`` for a whole subtree, without building child nodes."""
    total_bytes = 0
    total_files = 0
    for _path, size in iter_files(root):
        total_bytes += size
        total_files += 1
    return total_bytes, total_files


__all__ = ["find_large_files", "dir_sizes"]
