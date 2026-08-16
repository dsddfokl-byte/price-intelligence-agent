"""Single-process lock for the automated cycle."""

import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import IO, Optional


class LockAlreadyHeld(RuntimeError):
    """Raised when another cycle owns, or recently orphaned, the lock."""


class CycleLock:
    def __init__(self, path: Path, stale_after_seconds: int = 3600) -> None:
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        self._file: Optional[IO[str]] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise LockAlreadyHeld("Another cycle currently holds the lock") from None

        lock_file.seek(0, os.SEEK_END)
        has_metadata = lock_file.tell() > 0
        age_seconds = max(0.0, time.time() - self.path.stat().st_mtime)
        if has_metadata and age_seconds < self.stale_after_seconds:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise LockAlreadyHeld("A recent orphaned cycle lock is present")

        lock_file.seek(0)
        lock_file.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "token": uuid.uuid4().hex,
                "created_at": time.time(),
            },
            lock_file,
        )
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            self._file.truncate()
            self._file.flush()
            os.fsync(self._file.fileno())
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> "CycleLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
