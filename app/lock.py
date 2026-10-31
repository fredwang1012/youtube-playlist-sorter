"""Exclusive run lock so two real runs never overlap (an overlap could insert the same video twice)."""
import sys
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _lock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class AlreadyRunning(Exception):
    pass


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        if not f.tell() and not path.stat().st_size:
            f.write(b"\0")
            f.flush()
        try:
            _lock(f)
        except OSError:
            raise AlreadyRunning from None
        try:
            yield
        finally:
            _unlock(f)
    finally:
        f.close()
