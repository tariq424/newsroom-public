"""One Newsroom per PC: an exclusive OS lock on F:\\videos\\newsroom\\newsroom.lock, held for the life of the process.
A second copy can't take it and exits (so two copies never start jobs side by side). The OS drops the lock when the
process dies, so a crash never leaves a stale lock behind."""
import os
from pathlib import Path

_handle = None


def acquire(path):
    global _handle
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close(); return False
    f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()
    _handle = f
    return True
