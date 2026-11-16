"""An exclusive, single-host lock that keeps two collection runs off the same database.

The lock is an operating-system lock on a small file next to the database (``<db>.lock``):
``fcntl.flock`` on Linux/macOS, ``msvcrt.locking`` on Windows. The operating system releases it
when the holding process ends for any reason (normal exit, exception, crash, kill), so a crashed
run never leaves a permanent false lock. The file itself is left in place (deleting it would
race with another process opening it); its presence means nothing, only the lock does.

Limitations: one host only (no distributed locking), and network filesystems may not honour
these locks, so keep the database on a local disk. Importing this module creates nothing.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if os.name == "nt":
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # raises OSError when held

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises OSError when held

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class LockBusy(Exception):
    """Another process holds the lock. The message names the lock file only."""


def lock_path(db_path: str | Path) -> Path:
    """The lock file that guards the database at ``db_path``."""
    return Path(f"{Path(db_path)}.lock")


@contextmanager
def database_lock(db_path: str | Path) -> Iterator[Path]:
    """Hold the exclusive lock for ``db_path`` for the duration of the ``with`` block.

    Never waits: raises ``LockBusy`` at once if another process holds it, and ``OSError`` if
    the lock file cannot be created. Creates the database's directory if needed (as ``init_db``
    would). Yields the lock file path.
    """
    path = lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - closed in the finally block below
    try:
        try:
            _try_lock(handle)
        except OSError as exc:
            raise LockBusy(f"another collection run is using this database ({path})") from exc
        try:
            yield path
        finally:
            try:
                _unlock(handle)
            except OSError:
                pass  # closing the file (or the process ending) releases it anyway
    finally:
        handle.close()
