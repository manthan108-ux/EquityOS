"""Request spacing that holds across processes.

`PoliteClient` spaces requests to one host inside its own process. When pipeline stages run in child
processes (so a hung stage can be killed), each child has a fresh client, and without a shared
record two of them could call one site back to back. `SharedPacer` keeps the next allowed time per
host in a small file, guarded by a lock file, so every process on the machine waits its turn.

It only ever makes callers wait longer. A pacer that cannot be used (read-only folder, lock never
freed) returns no extra wait rather than stopping acquisition: the in-process spacing still applies.
"""

import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

LOCK_STALE_SECONDS = 10.0
LOCK_PATIENCE_SECONDS = 5.0


class SharedPacer:
    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.directory = Path(directory)
        self._clock, self._sleep = clock, sleep

    def _paths(self, host: str) -> tuple[Path, Path]:
        key = hashlib.sha1(host.lower().encode()).hexdigest()[:16]  # noqa: S324 - a file name
        return self.directory / f"{key}.next", self.directory / f"{key}.lock"

    def _lock(self, lock: Path) -> bool:
        waited = 0.0
        while waited < LOCK_PATIENCE_SECONDS:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    if self._clock() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                        lock.unlink(missing_ok=True)  # its holder died
                        continue
                except OSError:
                    pass
                self._sleep(0.02)
                waited += 0.02
        return False

    def reserve(self, host: str, interval: float, earliest: float = 0.0) -> float:
        """Claim the next slot for `host`; returns the seconds the caller must wait from now.

        `earliest` is a wait the caller already owes (its own in-process spacing); the slot is
        never earlier than that.
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            slot_file, lock = self._paths(host)
            if not self._lock(lock):
                return 0.0
            try:
                now = self._clock()
                try:
                    taken = float(slot_file.read_text() or 0)
                except (OSError, ValueError):
                    taken = 0.0
                start = max(now + earliest, taken)
                slot_file.write_text(repr(start + interval))
                return max(0.0, start - now)
            finally:
                lock.unlink(missing_ok=True)
        except OSError:
            return 0.0
