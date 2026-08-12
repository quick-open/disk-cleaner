"""Live sampling of system snapshots for the monitor view.

The GUI polls :func:`sample` on a background thread and marshals each snapshot
back to the UI.  :func:`sample` is a generator so a caller can iterate for as
long as it wants and stop simply by breaking out of the loop.
"""

from __future__ import annotations

import time

from . import sysinfo
from .errors import DiskKitError


def sample(interval=1.0, count=None, per_core=True, top=5):
    """Yield :func:`diskkit.sysinfo.snapshot` dicts every *interval* seconds.

    Yields forever when *count* is ``None``; otherwise yields exactly *count*
    snapshots.  The first snapshot is emitted immediately (CPU percentages are
    non-blocking deltas since the previous psutil call), then one per interval.
    """
    if interval < 0:
        raise DiskKitError("interval must be >= 0")
    emitted = 0
    while count is None or emitted < count:
        yield sysinfo.snapshot(per_core=per_core, top=top)
        emitted += 1
        if count is not None and emitted >= count:
            break
        if interval:
            time.sleep(interval)


def sample_for(seconds, interval=1.0, per_core=True, top=5):
    """Yield snapshots for approximately *seconds* wall-clock seconds."""
    if seconds < 0:
        raise DiskKitError("seconds must be >= 0")
    deadline = time.time() + seconds
    while True:
        yield sysinfo.snapshot(per_core=per_core, top=top)
        if time.time() >= deadline:
            break
        if interval:
            time.sleep(min(interval, max(0.0, deadline - time.time())))


__all__ = ["sample", "sample_for"]
