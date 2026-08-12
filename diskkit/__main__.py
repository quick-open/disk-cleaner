"""Command-line interface: ``python -m diskkit <command> ...``.

Safe by default: ``scan``/``usage``/``large``/``info``/``watch`` never modify
anything, and ``clean`` sends files to the Recycle Bin (recoverable) unless you
pass ``--no-trash``.  Destructive commands prompt for confirmation unless given
``--yes``.  A :class:`DiskKitError` becomes a clean non-zero exit, never a
traceback.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import (
    DiskKitError,
    TARGETS,
    TARGETS_BY_ID,
    clean_targets,
    dir_sizes,
    empty_recycle_bin,
    find_large_files,
    scan_targets,
    snapshot,
    target_ids,
)
from .common import human_size
from .monitor import sample_for


# --- helpers ----------------------------------------------------------------


def _parse_size(text):
    """Parse ``500``, ``10MB``, ``1.5g`` ... into a byte count."""
    if text is None:
        return 0
    s = str(text).strip().upper().replace(" ", "")
    if not s:
        return 0
    units = {"B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2,
             "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4}
    for suffix in ("TB", "GB", "MB", "KB", "T", "G", "M", "K", "B"):
        if s.endswith(suffix):
            num = s[: -len(suffix)] or "0"
            try:
                return int(float(num) * units[suffix])
            except ValueError:
                raise DiskKitError(f"invalid size: {text!r}")
    try:
        return int(float(s))
    except ValueError:
        raise DiskKitError(f"invalid size: {text!r}")


def _confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        reply = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def _resolve_target_ids(names):
    if not names:
        return None
    out = []
    for n in names:
        if n not in TARGETS_BY_ID:
            raise DiskKitError(
                f"unknown target {n!r}; choose from: {', '.join(target_ids())}")
        out.append(n)
    return out


# --- command handlers -------------------------------------------------------


def cmd_scan(a):
    selected = _resolve_target_ids(a.targets)
    report = scan_targets(selected)
    if a.json:
        print(json.dumps(report, indent=2))
        return
    total = 0
    total_files = 0
    print(f"{'TARGET':<28} {'FILES':>8} {'SIZE':>10}")
    print("-" * 48)
    for tid in (selected or target_ids()):
        if tid not in report:
            continue
        info = report[tid]
        if info.get("special"):
            print(f"{info['name']:<28} {'—':>8} {'(explicit)':>10}")
            continue
        total += info["bytes"]
        total_files += info["files"]
        print(f"{info['name']:<28} {info['files']:>8} {human_size(info['bytes']):>10}")
    print("-" * 48)
    print(f"{'TOTAL reclaimable':<28} {total_files:>8} {human_size(total):>10}")
    print("\nNothing was deleted (scan only). Use 'diskkit clean' to reclaim space.")


def cmd_clean(a):
    selected = _resolve_target_ids(a.targets)
    report = scan_targets(selected)
    total = sum(v["bytes"] for v in report.values() if not v.get("special"))
    names = [v["name"] for v in report.values() if not v.get("special") and v["files"]]
    if total == 0:
        print("Nothing to clean — selected targets are already empty.")
        return
    dest = "permanently deleted" if a.no_trash else "sent to the Recycle Bin"
    print(f"About to clean: {', '.join(names) or '(selected targets)'}")
    print(f"Reclaimable: {human_size(total)} — files will be {dest}.")
    if not _confirm("Proceed?", a.yes):
        print("Aborted. Nothing was deleted.")
        return
    freed = clean_targets(selected, to_trash=not a.no_trash)
    print(f"Done. Freed {human_size(freed)} ({dest}).")


def cmd_emptybin(a):
    if not _confirm("Permanently empty the Recycle Bin? This cannot be undone.", a.yes):
        print("Aborted.")
        return
    empty_recycle_bin(confirm=True)
    print("Recycle Bin emptied.")


def cmd_large(a):
    results = find_large_files(a.root, min_bytes=_parse_size(a.min), limit=a.limit)
    if a.json:
        print(json.dumps(results, indent=2))
        return
    if not results:
        print(f"No files >= {a.min} found under {a.root}.")
        return
    print(f"{'SIZE':>10}  PATH")
    print("-" * 60)
    for item in results:
        print(f"{human_size(item['bytes']):>10}  {item['path']}")
    print(f"\n{len(results)} file(s), largest first.")


def cmd_usage(a):
    tree = dir_sizes(a.root, depth=a.depth)
    if a.json:
        print(json.dumps(tree, indent=2))
        return
    print(f"{human_size(tree['bytes']):>10}  {tree['path']}  "
          f"({tree['files']} files)")
    _print_tree(tree, prefix="")


def _print_tree(node, prefix, level=0, max_level=None):
    children = node.get("children", [])
    for i, child in enumerate(children):
        last = i == len(children) - 1
        branch = "└─ " if last else "├─ "
        print(f"{human_size(child['bytes']):>10}  {prefix}{branch}{child['name']}")
        _print_tree(child, prefix + ("   " if last else "│  "), level + 1)


def cmd_info(a):
    snap = snapshot()
    if a.json:
        print(json.dumps(snap, indent=2))
        return
    cpu = snap["cpu"]
    mem = snap["memory"]
    print("== System snapshot ==")
    print(f"CPU     : {cpu['percent']:.0f}% over {cpu.get('count_logical')} "
          f"logical cores")
    vm = mem.get("virtual") or {}
    print(f"Memory  : {vm.get('percent', 0):.0f}% used "
          f"({human_size(vm.get('used', 0))} / {human_size(vm.get('total', 0))})")
    sw = mem.get("swap") or {}
    if sw.get("total"):
        print(f"Swap    : {sw.get('percent', 0):.0f}% used "
              f"({human_size(sw.get('used', 0))} / {human_size(sw.get('total', 0))})")
    bat = snap.get("battery")
    if bat:
        plug = "charging" if bat["plugged_in"] else "on battery"
        print(f"Battery : {bat['percent']:.0f}% ({plug})")
    print("\nDisks:")
    for p in snap["disks"]["partitions"]:
        print(f"  {p['mountpoint']:<16} {p['percent']:>5.0f}%  "
              f"{human_size(p['used'])} / {human_size(p['total'])}  ({p['fstype']})")
    print("\nTop processes by memory:")
    for proc in snap["top_memory"]:
        print(f"  {proc['pid']:>7}  {(proc['name'] or '')[:26]:<26} "
              f"{proc.get('memory_percent') or 0:>5.1f}%  "
              f"{human_size(proc.get('memory_rss') or 0)}")


def cmd_watch(a):
    interval = a.interval
    printed = 0
    for snap in sample_for(a.seconds, interval=interval):
        cpu = snap["cpu"]["percent"]
        vm = (snap["memory"].get("virtual") or {}).get("percent", 0)
        net = snap.get("net") or {}
        line = (f"cpu {cpu:>5.1f}%   mem {vm:>5.1f}%   "
                f"net ↓{human_size(net.get('bytes_recv', 0))} "
                f"↑{human_size(net.get('bytes_sent', 0))}")
        print(line)
        printed += 1
    if printed == 0:
        print("(no samples)")


# --- parser -----------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="diskkit",
        description="Offline disk cleaner & system info. Safe by default: "
        "scans never delete, and 'clean' sends files to the Recycle Bin.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help, handler):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(func=handler)
        return sp

    s = add("scan", "List cleanup targets and their reclaimable sizes (no delete)",
            cmd_scan)
    s.add_argument("--targets", nargs="+", metavar="ID",
                   help=f"limit to specific targets ({', '.join(target_ids())})")
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("clean", "Delete cleanup targets (to the Recycle Bin by default)",
            cmd_clean)
    s.add_argument("--targets", nargs="+", metavar="ID",
                   help="limit to specific targets (default: all)")
    s.add_argument("--no-trash", action="store_true",
                   help="delete permanently instead of using the Recycle Bin")
    s.add_argument("-y", "--yes", action="store_true",
                   help="do not prompt for confirmation")

    s = add("emptybin", "Permanently empty the Recycle Bin (Windows)", cmd_emptybin)
    s.add_argument("-y", "--yes", action="store_true", help="do not prompt")

    s = add("large", "Find the largest files under a folder", cmd_large)
    s.add_argument("root", help="folder to scan")
    s.add_argument("--min", default="0", help="minimum size, e.g. 10MB (default 0)")
    s.add_argument("--limit", type=int, default=50, help="max results (default 50)")
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("usage", "Show folder sizes under a directory (usage view)", cmd_usage)
    s.add_argument("root", help="folder to analyse")
    s.add_argument("--depth", type=int, default=1, help="levels to expand (default 1)")
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("info", "Print a one-shot system snapshot", cmd_info)
    s.add_argument("--json", action="store_true", help="emit JSON")

    s = add("watch", "Live CPU/memory/network for N seconds", cmd_watch)
    s.add_argument("seconds", type=float, nargs="?", default=5.0,
                   help="how long to watch (default 5)")
    s.add_argument("--interval", type=float, default=1.0,
                   help="seconds between samples (default 1)")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except DiskKitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
