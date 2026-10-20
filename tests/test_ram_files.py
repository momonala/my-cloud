"""Tests for src/core/ram_files.py: what reaches disk, and when."""

import os

from src.core.ram_files import RamMirror


def _mirror(tmp_path, **kwargs) -> RamMirror:
    disk = tmp_path / "disk"
    disk.mkdir(exist_ok=True)
    return RamMirror(disk, "app", ("*.session",), ram_root=tmp_path / "run", **kwargs)


def test_open_seeds_ram_with_matching_disk_files(tmp_path):
    mirror = _mirror(tmp_path)
    (mirror.disk_dir / "a.session").write_text("disk")
    (mirror.disk_dir / "ignored.txt").write_text("disk")

    ram = mirror.open()

    assert ram == tmp_path / "run" / "app"
    assert (ram / "a.session").read_text() == "disk"
    assert not (ram / "ignored.txt").exists()


def test_flush_writes_only_files_whose_content_changed(tmp_path):
    mirror = _mirror(tmp_path)
    for name in ("same.session", "changed.session"):
        (mirror.disk_dir / name).write_text("v1")
    ram = mirror.open()
    (ram / "changed.session").write_text("v2")
    (ram / "same.session").write_text("v1")
    untouched_inode = (mirror.disk_dir / "same.session").stat().st_ino

    mirror.flush()

    assert (mirror.disk_dir / "changed.session").read_text() == "v2"
    assert (mirror.disk_dir / "same.session").stat().st_ino == untouched_inode


def test_maybe_flush_waits_for_the_interval(tmp_path):
    mirror = _mirror(tmp_path, flush_interval_seconds=3600)
    (mirror.open() / "a.session").write_text("new")

    mirror.maybe_flush()

    assert not (mirror.disk_dir / "a.session").exists()


def test_without_overwrite_flush_only_restores_missing_files(tmp_path):
    mirror = _mirror(tmp_path, overwrite=False)
    (mirror.disk_dir / "kept.session").write_text("v1")
    ram = mirror.open()
    (ram / "kept.session").write_text("v2")
    (ram / "downloaded.session").write_text("libs")

    mirror.flush()

    assert (mirror.disk_dir / "kept.session").read_text() == "v1"
    assert (mirror.disk_dir / "downloaded.session").read_text() == "libs"


def test_without_a_runtime_directory_works_on_disk_directly(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    disk = tmp_path / "disk"

    assert RamMirror(disk, "app", ("*.session",)).open() == disk


def test_flush_leaves_a_disk_file_changed_by_someone_else(tmp_path):
    mirror = _mirror(tmp_path)
    (mirror.disk_dir / "a.session").write_text("v1")
    ram = mirror.open()
    (ram / "a.session").write_text("stale")
    (mirror.disk_dir / "a.session").write_text("copied from the Mac")
    os.utime(mirror.disk_dir / "a.session", (1, 1))

    mirror.flush()

    assert (mirror.disk_dir / "a.session").read_text() == "copied from the Mac"
