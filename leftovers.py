"""What earlier runs left in the temporary folder.

The attachment viewer keeps copies of the parts it shows in a folder of its
own there, and the updater opens its disk image on one. Both are cleared as
they go, but a run that crashes or is made to quit cannot clear up after
itself, and its folders stayed. Each viewer's folder is claimed with the
process that made it, and at start whatever no running process claims goes.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

#: The folders this app makes in the temporary folder.
PREFIXES = ("mm-attach-", "mm-update-")
#: Inside a folder, which process it belongs to.
CLAIM = ".owner"
#: How long an unclaimed folder is left, for one made by a version that did
#: not claim its folders and may still be running.
UNCLAIMED_FOR = 3600.0


def claim(folder: Path) -> None:
    """Mark ``folder`` as this process's."""
    try:
        (Path(folder) / CLAIM).write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


def _running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # somebody else's process: running, and not ours
    return True


def _owner(folder: Path) -> Optional[int]:
    try:
        return int((folder / CLAIM).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def sweep(place: Optional[Path] = None, now: Optional[float] = None) -> int:
    """Remove what earlier runs left in ``place`` (the temporary folder):
    folders whose process has gone, and unclaimed ones untouched for
    UNCLAIMED_FOR. Never one still mounted, nor anybody else's. Returns how
    many went."""
    place = Path(place or tempfile.gettempdir())
    now = time.time() if now is None else now
    try:
        entries = list(os.scandir(place))
    except OSError:
        return 0
    gone = 0
    for entry in entries:
        if not entry.name.startswith(PREFIXES):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        folder = Path(entry.path)
        if stat.st_uid != os.getuid() or os.path.ismount(folder):
            continue
        owner = _owner(folder)
        if owner is not None:
            if owner == os.getpid() or _running(owner):
                continue
        elif now - stat.st_mtime < UNCLAIMED_FOR:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            gone += 1
    return gone
