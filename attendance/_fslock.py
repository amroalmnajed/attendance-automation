"""attendance/_fslock.py — Cross-process advisory file lock + atomic JSON write (stdlib only, Windows-safe)."""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator


@contextlib.contextmanager
def file_lock(target: Path, *, timeout_s: float = 10.0, stale_s: float = 30.0) -> Iterator[None]:
    """Exclusive lock via O_EXCL lockfile `<target>.lock`. Breaks locks older than stale_s."""
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_s:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Could not acquire lock {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to a temp file then os.replace (atomic on POSIX and Windows)."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
