"""The background agent's job, and where it is written.

launchd starts the job with none of the environment it was set up in. One
written from a moved home (every test moves it) would run against the
person's real account, so from there it is written beside that home and
launchd is never asked.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

import scheduler

JOB = f"{scheduler.LAUNCH_AGENT_LABEL}.plist"


@pytest.fixture
def launchctl(monkeypatch):
    """Every launchctl command, recorded instead of run."""
    asked = []
    run = subprocess.run

    def recorded(command, *args, **kwargs):
        if command and Path(command[0]).name == "launchctl":
            asked.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorded)
    return asked


@pytest.fixture
def person(tmp_path, monkeypatch):
    """A home standing in for the real one, so nothing here can reach it."""
    home = tmp_path / "person"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


class TestFromAMovedHome:
    def test_the_job_is_written_beside_it(self, isolated_home, person, launchctl):
        ok, message = scheduler.install_agent(1440)
        assert ok, message
        moved = isolated_home / "app-home"
        job = plistlib.loads((moved / "LaunchAgents" / JOB).read_bytes())
        assert job["ProgramArguments"] == scheduler.agent_command()
        assert job["StartInterval"] == 86400
        assert job["StandardOutPath"] == str(moved / "Logs" / "agent.out.log")

    def test_nothing_reaches_the_real_launch_agents(self, person, launchctl):
        scheduler.install_agent(60)
        assert not (person / "Library").exists()

    def test_launchd_is_never_asked(self, person, launchctl):
        scheduler.install_agent(60)
        scheduler.install_agent(180)
        scheduler.remove_agent()
        assert launchctl == []

    def test_turning_it_off_takes_it_away_again(self, person, launchctl):
        scheduler.install_agent(60)
        assert scheduler.agent_installed()
        assert scheduler.remove_agent() == (True, "Background scanning is off.")
        assert not scheduler.agent_installed()


class TestFromTheAppsOwnHome:
    @pytest.fixture(autouse=True)
    def own_home(self, monkeypatch):
        monkeypatch.delenv("ICLOUD_TRIAGE_HOME")

    def test_the_job_goes_to_launch_agents_and_launchd_starts_it(
            self, person, launchctl):
        ok, message = scheduler.install_agent(180)
        assert ok, message
        job = person / "Library" / "LaunchAgents" / JOB
        assert plistlib.loads(job.read_bytes())["StartInterval"] == 10800
        domain = f"gui/{os.getuid()}"
        assert launchctl == [
            ["launchctl", "bootout", f"{domain}/{scheduler.LAUNCH_AGENT_LABEL}"],
            ["launchctl", "bootstrap", domain, str(job)],
        ]

    def test_turning_it_off_stops_it_and_removes_the_job(self, person, launchctl):
        scheduler.install_agent(180)
        scheduler.remove_agent()
        assert not (person / "Library" / "LaunchAgents" / JOB).exists()
        assert [command[1] for command in launchctl] == [
            "bootout", "bootstrap", "bootout"]
