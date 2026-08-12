"""System information snapshots via :mod:`psutil`.

Everything here returns plain, JSON-serialisable dicts (numbers, strings, lists
and nested dicts only) so the CLI can ``json.dumps`` a snapshot and the GUI can
poll it on a thread without touching live psutil objects.  Every field is
individually guarded: a platform that lacks battery info, swap, or IO counters
yields ``None`` for that field instead of raising.
"""

from __future__ import annotations

import time

try:  # psutil is a hard runtime dep, but keep import failure legible.
    import psutil
except Exception as exc:  # pragma: no cover - dependency missing
    psutil = None
    _PSUTIL_ERROR = exc
else:
    _PSUTIL_ERROR = None

from .errors import DiskKitError


def _require_psutil():
    if psutil is None:  # pragma: no cover - dependency missing
        raise DiskKitError(f"psutil is required for system info: {_PSUTIL_ERROR}")


def _safe(fn, default=None):
    """Call *fn* and return its result, or *default* on any failure."""
    try:
        return fn()
    except Exception:
        return default


def cpu_info(per_core=True):
    """CPU load: overall percent, per-core percents, counts and frequency."""
    _require_psutil()
    info = {
        "percent": _safe(lambda: psutil.cpu_percent(interval=None), 0.0),
        "count_logical": _safe(lambda: psutil.cpu_count(logical=True)),
        "count_physical": _safe(lambda: psutil.cpu_count(logical=False)),
    }
    if per_core:
        info["per_core"] = _safe(
            lambda: psutil.cpu_percent(interval=None, percpu=True), []) or []
    freq = _safe(lambda: psutil.cpu_freq())
    info["freq_mhz"] = round(freq.current, 1) if freq else None
    load = _safe(lambda: psutil.getloadavg())
    info["load_avg"] = list(load) if load else None
    return info


def memory_info():
    """Virtual memory and swap usage, in bytes plus percent."""
    _require_psutil()
    vm = _safe(lambda: psutil.virtual_memory())
    sm = _safe(lambda: psutil.swap_memory())
    mem = {}
    if vm is not None:
        mem["virtual"] = {
            "total": vm.total, "available": vm.available,
            "used": vm.used, "free": getattr(vm, "free", None),
            "percent": vm.percent,
        }
    else:  # pragma: no cover
        mem["virtual"] = None
    if sm is not None:
        mem["swap"] = {
            "total": sm.total, "used": sm.used,
            "free": sm.free, "percent": sm.percent,
        }
    else:
        mem["swap"] = None
    return mem


def disks():
    """Mounted partitions with usage, plus per-disk IO counters.

    Returns ``{"partitions": [...], "io": {name: {...}}}``.  Partitions that
    cannot be stat'd (e.g. an empty CD drive) are skipped.
    """
    _require_psutil()
    parts = []
    for part in _safe(lambda: psutil.disk_partitions(all=False), []) or []:
        usage = _safe(lambda: psutil.disk_usage(part.mountpoint))
        if usage is None:
            continue
        parts.append({
            "device": part.device,
            "mountpoint": part.mountpoint,
            "fstype": part.fstype,
            "opts": part.opts,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        })
    io = {}
    counters = _safe(lambda: psutil.disk_io_counters(perdisk=True), {}) or {}
    for name, c in counters.items():
        io[name] = {
            "read_bytes": c.read_bytes, "write_bytes": c.write_bytes,
            "read_count": c.read_count, "write_count": c.write_count,
        }
    return {"partitions": parts, "io": io}


def net_info():
    """Aggregate network IO counters (bytes/packets sent and received)."""
    _require_psutil()
    c = _safe(lambda: psutil.net_io_counters())
    if c is None:  # pragma: no cover
        return None
    return {
        "bytes_sent": c.bytes_sent, "bytes_recv": c.bytes_recv,
        "packets_sent": c.packets_sent, "packets_recv": c.packets_recv,
        "errin": c.errin, "errout": c.errout,
        "dropin": c.dropin, "dropout": c.dropout,
    }


def top_processes(limit=5, by="memory"):
    """Top processes by ``memory`` or ``cpu``.

    Returns a list of ``{"pid", "name", "cpu_percent", "memory_percent",
    "memory_rss"}`` dicts, biggest first.  Processes that vanish mid-scan are
    skipped.
    """
    _require_psutil()
    procs = []
    fields = ["pid", "name", "memory_percent", "memory_info"]
    if by == "cpu":
        fields.append("cpu_percent")
    for p in _safe(lambda: list(psutil.process_iter(fields)), []) or []:
        try:
            info = p.info
            rss = None
            mi = info.get("memory_info")
            if mi is not None:
                rss = getattr(mi, "rss", None)
            procs.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "",
                "cpu_percent": info.get("cpu_percent"),
                "memory_percent": info.get("memory_percent") or 0.0,
                "memory_rss": rss,
            })
        except Exception:
            continue
    key = "cpu_percent" if by == "cpu" else "memory_percent"
    procs.sort(key=lambda d: (d.get(key) or 0.0), reverse=True)
    return procs[: max(0, int(limit))]


def battery():
    """Battery status, or ``None`` on machines without one."""
    _require_psutil()
    fn = getattr(psutil, "sensors_battery", None)
    if fn is None:
        return None
    b = _safe(fn)
    if b is None:
        return None
    secs = b.secsleft
    if secs in (getattr(psutil, "POWER_TIME_UNLIMITED", -1),
                getattr(psutil, "POWER_TIME_UNKNOWN", -2)):
        secs = None
    return {
        "percent": b.percent,
        "plugged_in": bool(b.power_plugged),
        "secs_left": secs,
    }


def snapshot(per_core=True, top=5):
    """A full, JSON-serialisable system snapshot.

    Keys: ``timestamp``, ``boot_time``, ``uptime_secs``, ``cpu``, ``memory``,
    ``disks``, ``net``, ``top_memory``, ``top_cpu``, ``battery``.
    """
    _require_psutil()
    boot = _safe(lambda: psutil.boot_time())
    now = time.time()
    return {
        "timestamp": now,
        "boot_time": boot,
        "uptime_secs": (now - boot) if boot else None,
        "cpu": cpu_info(per_core=per_core),
        "memory": memory_info(),
        "disks": disks(),
        "net": net_info(),
        "top_memory": top_processes(limit=top, by="memory"),
        "top_cpu": top_processes(limit=top, by="cpu"),
        "battery": battery(),
    }


__all__ = [
    "snapshot", "cpu_info", "memory_info", "disks", "net_info",
    "top_processes", "battery",
]
