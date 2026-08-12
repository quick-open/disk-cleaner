"""Headless, deterministic tests for the diskkit core.

Nothing here deletes anything outside a pytest ``tmp_path``.  Cleaning is
exercised with ``to_trash=False`` (permanent delete) so the tests do not depend
on a real Recycle Bin / a working ``send2trash`` on a headless CI box.
"""

from __future__ import annotations

import json
import os

import pytest

_IS_WINDOWS = os.name == "nt"

from diskkit import (
    DiskKitError,
    clean_targets,
    dir_sizes,
    find_large_files,
    scan_targets,
    snapshot,
    target_ids,
)
from diskkit import cleaners
from diskkit.common import expand, human_size


# --- fixtures ---------------------------------------------------------------


def _write(path, size):
    """Create *path* (and parents) holding exactly *size* bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


@pytest.fixture
def tree(tmp_path):
    """A small deterministic tree, total 6000 bytes across 4 files.

        root/
          a.bin           1000
          sub/b.bin       2000
          sub/c.bin        500
          sub/deep/d.bin  2500
    """
    _write(str(tmp_path / "a.bin"), 1000)
    _write(str(tmp_path / "sub" / "b.bin"), 2000)
    _write(str(tmp_path / "sub" / "c.bin"), 500)
    _write(str(tmp_path / "sub" / "deep" / "d.bin"), 2500)
    return tmp_path


# --- common helpers ---------------------------------------------------------


def test_human_size():
    assert human_size(0) == "0B"
    assert human_size(1023) == "1023B"
    assert human_size(1024) == "1.0KB"
    assert human_size(1024 * 1024) == "1.0MB"


def test_expand_windows_style():
    env = {"TEMP": "/tmp/x", "LOCALAPPDATA": "/home/u/AppData/Local"}
    assert expand(r"%TEMP%\foo", env=env) == "/tmp/x\\foo"
    assert expand(r"%LOCALAPPDATA%\Temp", env=env) == "/home/u/AppData/Local\\Temp"
    # Unknown var is left intact rather than blanked.
    assert "%NOPE%" in expand(r"%NOPE%\x", env=env)


# --- scan_targets -----------------------------------------------------------


def test_scan_targets_counts_and_bytes(tree):
    roots = {"user_temp": [str(tree)]}
    report = scan_targets(["user_temp"], roots=roots)
    info = report["user_temp"]
    assert info["files"] == 4
    assert info["bytes"] == 6000
    assert info["name"]  # human label present


def test_scan_targets_is_read_only(tree):
    roots = {"user_temp": [str(tree)]}
    scan_targets(["user_temp"], roots=roots)
    # Every file still there, untouched.
    assert (tree / "a.bin").exists()
    assert (tree / "sub" / "deep" / "d.bin").exists()


def test_scan_empty_target_is_zero(tmp_path):
    roots = {"user_temp": [str(tmp_path)]}
    report = scan_targets(["user_temp"], roots=roots)
    assert report["user_temp"]["files"] == 0
    assert report["user_temp"]["bytes"] == 0


def test_scan_dedupes_overlapping_specs(tmp_path):
    # A target whose specs resolve to the SAME directory must not double-count.
    # Two identical roots (via the roots override) exercise the dedupe path on
    # every OS without depending on platform-specific env-var expansion.
    _write(str(tmp_path / "x.bin"), 400)
    roots = {"user_temp": [str(tmp_path), str(tmp_path)]}
    report = scan_targets(["user_temp"], roots=roots)
    assert report["user_temp"]["files"] == 1
    assert report["user_temp"]["bytes"] == 400


def test_scan_unknown_target_raises():
    with pytest.raises(DiskKitError):
        scan_targets(["does_not_exist"])


def test_scan_all_targets_match_target_ids():
    report = scan_targets(None)
    assert set(report) == set(target_ids())


@pytest.mark.skipif(not _IS_WINDOWS, reason="Recycle Bin is a Windows-only target")
def test_scan_all_targets_includes_recycle_bin():
    report = scan_targets(None)
    assert report["recycle_bin"]["special"] == "recycle_bin"


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX (Linux/macOS) target table")
def test_posix_target_table_is_linux_shaped():
    """On AIQuick/Linux the table auto-detects and exposes XDG/Unix targets."""
    ids = set(target_ids())
    # Windows-only ids must be gone; Unix ids present.
    assert "recycle_bin" not in ids
    assert "windows_temp" not in ids
    assert {"user_temp", "trash", "thumbnail_cache", "pip_cache"} <= ids
    # No target may carry a Windows drive letter, backslash, or %VAR% spec.
    for target in cleaners.TARGETS:
        for spec in target.patterns:
            assert "\\" not in spec, spec
            assert "%" not in spec, spec
            assert not (len(spec) > 1 and spec[1] == ":"), spec  # no C:/D: drives
        assert target.special is None  # no shell-special targets on POSIX
    # The trash target points at the freedesktop location, not a Recycle Bin.
    trash = cleaners.TARGETS_BY_ID["trash"]
    assert any(".local/share/Trash" in p or "XDG_DATA_HOME" in p
               for p in trash.patterns)


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX (Linux/macOS) target table")
def test_posix_cache_target_resolves_via_xdg(tmp_path, monkeypatch):
    """A real Linux cache path (pip) is auto-detected and sized correctly."""
    # Isolate both the XDG cache dir and HOME so the real ~/.cache/pip on the
    # host machine is never counted.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(str(tmp_path / "pip" / "wheels" / "w.bin"), 700)
    report = scan_targets(["pip_cache"])
    assert report["pip_cache"]["files"] == 1
    assert report["pip_cache"]["bytes"] == 700


def test_windows_target_table_intact():
    """The Windows table (used when os.name=='nt') still carries its targets."""
    ids = {t.id for t in cleaners._WINDOWS_TARGETS}
    assert {"user_temp", "windows_temp", "recycle_bin"} <= ids
    rb = next(t for t in cleaners._WINDOWS_TARGETS if t.id == "recycle_bin")
    assert rb.special == "recycle_bin"


# --- clean_targets ----------------------------------------------------------


def test_clean_targets_removes_and_reports_freed(tree):
    roots = {"user_temp": [str(tree)]}
    before = scan_targets(["user_temp"], roots=roots)["user_temp"]["bytes"]
    assert before == 6000
    freed = clean_targets(["user_temp"], to_trash=False, roots=roots)
    assert freed == 6000
    # The target's children are gone...
    assert not (tree / "a.bin").exists()
    assert not (tree / "sub").exists()
    # ...but the target root directory itself is preserved (emptied, not removed).
    assert tree.exists()
    after = scan_targets(["user_temp"], roots=roots)["user_temp"]["bytes"]
    assert after == 0


def test_clean_only_touches_selected_root(tmp_path):
    target_root = tmp_path / "cache"
    bystander = tmp_path / "keep"
    _write(str(target_root / "junk.tmp"), 800)
    _write(str(bystander / "important.txt"), 1234)
    roots = {"user_temp": [str(target_root)]}
    freed = clean_targets(["user_temp"], to_trash=False, roots=roots)
    assert freed == 800
    assert not (target_root / "junk.tmp").exists()
    assert (bystander / "important.txt").exists()  # never in scope


@pytest.mark.skipif(not _IS_WINDOWS, reason="Recycle Bin is a Windows-only target")
def test_clean_special_target_is_noop(tree):
    # Recycle Bin is special: clean_targets ignores it (no crash, frees 0).
    freed = clean_targets(["recycle_bin"], to_trash=False)
    assert freed == 0


def test_empty_recycle_bin_requires_confirm():
    with pytest.raises(DiskKitError):
        cleaners.empty_recycle_bin(confirm=False)


# --- find_large_files -------------------------------------------------------


def test_find_large_files_threshold_and_order(tree):
    res = find_large_files(str(tree), min_bytes=1000, limit=100)
    sizes = [r["bytes"] for r in res]
    assert sizes == [2500, 2000, 1000]          # sorted desc, >=1000 only
    assert all(r["bytes"] >= 1000 for r in res)
    assert res[0]["name"] == "d.bin"


def test_find_large_files_limit(tree):
    res = find_large_files(str(tree), min_bytes=0, limit=2)
    assert len(res) == 2
    assert res[0]["bytes"] == 2500
    assert res[1]["bytes"] == 2000


def test_find_large_files_bad_root(tmp_path):
    with pytest.raises(DiskKitError):
        find_large_files(str(tmp_path / "nope"), min_bytes=0)


# --- dir_sizes --------------------------------------------------------------


def test_dir_sizes_totals(tree):
    node = dir_sizes(str(tree), depth=2)
    assert node["bytes"] == 6000
    assert node["files"] == 4
    # children sorted largest first: sub (5000) then a.bin is a file (not a child)
    child_names = [c["name"] for c in node["children"]]
    assert child_names == ["sub"]
    sub = node["children"][0]
    assert sub["bytes"] == 5000
    assert sub["files"] == 3


def test_dir_sizes_depth_zero_totals_only(tree):
    node = dir_sizes(str(tree), depth=0)
    assert node["bytes"] == 6000        # totals still correct
    assert node["children"] == []       # but no child nodes materialised


def test_dir_sizes_children_sorted_desc(tree):
    # Add a second sibling folder so ordering is observable.
    _write(str(tree / "big" / "x.bin"), 9000)
    node = dir_sizes(str(tree), depth=1)
    names = [c["name"] for c in node["children"]]
    assert names == ["big", "sub"]      # 9000 before 5000


# --- sysinfo ----------------------------------------------------------------


def test_snapshot_keys_and_json_serialisable():
    snap = snapshot()
    for key in ("timestamp", "boot_time", "cpu", "memory", "disks",
                "net", "top_memory", "top_cpu", "battery"):
        assert key in snap
    assert "percent" in snap["cpu"]
    assert "virtual" in snap["memory"]
    assert isinstance(snap["disks"]["partitions"], list)
    # The whole thing must round-trip through JSON (GUI/CLI rely on this).
    text = json.dumps(snap)
    again = json.loads(text)
    assert again["cpu"]["percent"] == snap["cpu"]["percent"]


def test_snapshot_top_processes_shape():
    snap = snapshot(top=3)
    assert len(snap["top_memory"]) <= 3
    for proc in snap["top_memory"]:
        assert set(("pid", "name", "memory_percent")).issubset(proc)
