from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class AlreadyRunningError(RuntimeError):
    """Raised when another process owns the task lock."""


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        if self._handle.seek(0, os.SEEK_END) == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise AlreadyRunningError("已有任务正在运行") from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
