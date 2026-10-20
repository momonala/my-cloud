"""RAM working copies of files that libraries rewrite far more often than they change.

pyicloud rewrites its cookie and session files after every HTTP request, and
the Anisette provider re-saves its 2 MB library bundle on every VM rebuild. On
the Pi that is a steady stream of SD card writes for files whose content
rarely changes. `RamMirror` points the library at a copy in the tmpfs that
systemd's `RuntimeDirectory=` provides (`$RUNTIME_DIRECTORY`), and writes back
to disk only when the content differs.

Without `$RUNTIME_DIRECTORY` (the Macs, tests, CLI runs) the disk directory is
used directly, so first-time login and 2FA always land on disk.
"""

import atexit
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path


def _runtime_directory() -> Path | None:
    runtime_directory = os.environ.get("RUNTIME_DIRECTORY")
    return Path(runtime_directory) if runtime_directory else None


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` so a crash leaves the old or new file, never half of one."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class RamMirror:
    """A RAM directory seeded from `disk_dir`, holding the files matching `patterns`.

    With `overwrite=False`, `flush` only restores files missing from disk: for
    content that is fixed once downloaded, but that a library re-saves anyway.
    """

    def __init__(
        self,
        disk_dir: Path,
        name: str,
        patterns: tuple[str, ...],
        *,
        overwrite: bool = True,
        flush_interval_seconds: float = 0,
        ram_root: Path | None = None,
    ) -> None:
        ram_root = ram_root or _runtime_directory()
        self.disk_dir = disk_dir
        self.dir = ram_root / name if ram_root else disk_dir
        self._patterns = patterns
        self._overwrite = overwrite
        self._flush_interval_seconds = flush_interval_seconds
        self._last_flush = time.monotonic()
        self._seeded = False
        self._disk_mtimes: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def in_ram(self) -> bool:
        return self.dir != self.disk_dir

    def open(self) -> Path:
        """Return the working directory, copying the disk files into RAM on first call."""
        with self._lock:
            if self.in_ram and not self._seeded:
                self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                for disk_file in self._files(self.disk_dir):
                    shutil.copy2(disk_file, self.dir / disk_file.name)
                    self._disk_mtimes[disk_file.name] = disk_file.stat().st_mtime
                atexit.register(self.flush)
                self._seeded = True
        return self.dir

    def flush(self) -> None:
        """Write RAM files back to disk where they differ (or are missing, with `overwrite=False`).

        A disk file changed by someone else since it was copied in, e.g. a session copied over
        from the Mac, is left alone.
        """
        with self._lock:
            self._last_flush = time.monotonic()
            if not self.in_ram:
                return
            for ram_file in self._files(self.dir):
                disk_file = self.disk_dir / ram_file.name
                if disk_file.exists() and not self._may_overwrite(disk_file):
                    continue
                data = ram_file.read_bytes()
                if not data or (disk_file.exists() and disk_file.read_bytes() == data):
                    continue
                write_atomic(disk_file, data)
                self._disk_mtimes[ram_file.name] = disk_file.stat().st_mtime

    def _may_overwrite(self, disk_file: Path) -> bool:
        return self._overwrite and disk_file.stat().st_mtime == self._disk_mtimes.get(disk_file.name)

    def maybe_flush(self) -> None:
        """`flush`, if at least `flush_interval_seconds` have passed since the last one."""
        if time.monotonic() - self._last_flush >= self._flush_interval_seconds:
            self.flush()

    def _files(self, directory: Path) -> list[Path]:
        return [path for pattern in self._patterns for path in directory.glob(pattern) if path.is_file()]
