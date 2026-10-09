"""The screen's turns (conftest.take_the_screen): tests that show windows on
the real screen share it, and one that needs its app to stay in front has it
alone, first. Each case runs in processes of its own, as the test workers
are, with lock files of its own."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: Takes the screen at the moment it is given, holds it, gives it back.
TAKER = textwrap.dedent("""
    import fcntl, json, sys, time
    sys.path.insert(0, sys.argv[1])
    import conftest
    alone, at, hold = sys.argv[2] == "alone", float(sys.argv[3]), float(sys.argv[4])
    time.sleep(max(0.0, at - time.monotonic()))
    asked = time.monotonic()
    conftest.take_the_screen(alone=alone)
    got = time.monotonic()
    time.sleep(hold)
    for key in ("screen", "gate"):
        held = conftest._SCREEN.pop(key, None)
        if held is not None:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
    print(json.dumps({"asked": asked, "got": got, "done": time.monotonic()}))
""")


def _turns(tmp_path, *takers):
    """(kind, start after, hold) for each process; what each saw."""
    start = time.monotonic() + 3.0      # after every process has started
    env = {"TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    running = [subprocess.Popen([sys.executable, "-c", TAKER, str(TESTS), kind,
                                 str(start + after), str(hold)],
                                stdout=subprocess.PIPE, text=True, env=env,
                                cwd=str(TESTS.parent))
               for kind, after, hold in takers]
    return [json.loads(process.communicate(timeout=60)[0]) for process in running]


def test_tests_that_share_the_screen_do_not_wait_for_each_other(tmp_path):
    first, second = _turns(tmp_path, ("shared", 0.0, 2.0), ("shared", 0.5, 0.0))
    assert second["got"] - second["asked"] < 0.5
    assert second["got"] < first["done"]


def test_one_that_has_it_alone_keeps_the_others_off(tmp_path):
    alone, shared = _turns(tmp_path, ("alone", 0.0, 2.0), ("shared", 0.5, 0.0))
    assert shared["got"] >= alone["done"] - 0.1


def test_one_that_wants_it_alone_waits_for_the_others(tmp_path):
    shared, alone = _turns(tmp_path, ("shared", 0.0, 2.0), ("alone", 0.5, 0.0))
    assert alone["got"] >= shared["done"] - 0.1


def test_one_that_wants_it_alone_goes_before_those_asking_after_it(tmp_path):
    """Or a run of shared tests would keep it waiting past its time."""
    first, alone, later = _turns(tmp_path, ("shared", 0.0, 2.0), ("alone", 0.5, 1.0),
                                 ("shared", 1.0, 0.0))
    assert alone["got"] >= first["done"] - 0.1
    assert later["got"] >= alone["done"] - 0.1
