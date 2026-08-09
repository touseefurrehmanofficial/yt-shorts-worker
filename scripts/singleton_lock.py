"""Guards against a second, fully independent copy of a script running at
once. This machine has been observed to spontaneously duplicate every
venv-launched python.exe process into a second, independent process (via the
system-wide Python install) -- confirmed unrelated to any code in this
project (it reproduces with a one-line script and no imports at all). For a
Flask dashboard that duplication is harmless (only one copy ever binds the
port), but for pipeline.py/fb_scraper.py a ghost duplicate would race the
real process on data/reels.csv and on the shared Playwright browser
profiles. acquire() below makes the loser exit immediately instead.
"""
import msvcrt
from pathlib import Path

LOCKS_DIR = Path(__file__).resolve().parent.parent / "logs"


def acquire(name: str):
    """Returns an open file handle holding an exclusive lock for `name`, or
    None if another live process already holds it. The OS releases the lock
    automatically when the holding process exits or is killed, so it can
    never get stuck stale -- keep the returned handle open for as long as
    the script should be considered 'running'."""
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS_DIR / f"{name}.lock"
    lock_file = open(lock_path, "w")
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_file.close()
        return None
    return lock_file
