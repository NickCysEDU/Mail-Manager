"""Which build this is. A version number alone does not identify one.

Every change between releases carries the same version, so a bug report that
says "1.0.0" says almost nothing. The commit is the part that answers "which
code was this?", and it has to survive being frozen into an app where there is
no git to ask.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import buildinfo
from models import APP_VERSION


class TestWhatItReports:
    def test_the_short_form_always_has_the_version(self):
        assert buildinfo.short().startswith(APP_VERSION)

    def test_the_full_form_names_the_interpreter_and_the_architecture(self):
        described = buildinfo.full()
        assert APP_VERSION in described
        assert "Python" in described
        assert any(word in described for word in
                   ("Apple silicon", "Intel", "arm64", "x86_64"))

    def test_a_checkout_reports_its_commit(self):
        """Skipped rather than failed where git is not available."""
        reference = buildinfo.commit()
        if not reference:
            pytest.skip("not a git checkout")
        assert 6 <= len(reference) <= 60
        assert buildinfo.short() == f"{APP_VERSION} · {reference}"


class TestAFrozenApp:
    """No git inside a .app, so the build script writes the answer down."""

    def test_a_stamp_is_written_and_read_back(self, tmp_path, monkeypatch):
        stamp = tmp_path / buildinfo.STAMP_FILE
        written = buildinfo.write_stamp(stamp, "abc1234")
        assert stamp.exists() and "abc1234" in written

        buildinfo._baked.cache_clear()
        buildinfo._from_git.cache_clear()
        monkeypatch.setattr(buildinfo, "_root", lambda: tmp_path)
        try:
            assert buildinfo.commit() == "abc1234"
            assert buildinfo.built_on()
            assert "built " in buildinfo.full()
        finally:
            buildinfo._baked.cache_clear()
            buildinfo._from_git.cache_clear()

    def test_no_stamp_and_no_git_still_gives_a_version(self, tmp_path, monkeypatch):
        buildinfo._baked.cache_clear()
        buildinfo._from_git.cache_clear()
        original = buildinfo._from_git
        monkeypatch.setattr(buildinfo, "_root", lambda: tmp_path)
        monkeypatch.setattr(buildinfo, "_from_git", lambda: "")
        try:
            assert buildinfo.short() == APP_VERSION
            assert APP_VERSION in buildinfo.full()
        finally:
            buildinfo._baked.cache_clear()
            original.cache_clear()

    def test_git_is_not_consulted_from_a_frozen_app(self, monkeypatch):
        """Shelling out inside a .app is slow and always fails anyway."""
        monkeypatch.setattr(buildinfo.sys, "frozen", True, raising=False)
        buildinfo._from_git.cache_clear()
        try:
            assert buildinfo._from_git() == ""
        finally:
            monkeypatch.delattr(buildinfo.sys, "frozen", raising=False)
            buildinfo._from_git.cache_clear()


class TestItNeverGetsInTheWay:
    def test_nothing_raises_even_with_git_missing(self, monkeypatch):
        def explode(*_args, **_kwargs):
            raise OSError("no git here")

        monkeypatch.setattr(buildinfo.subprocess, "run", explode)
        buildinfo._from_git.cache_clear()
        try:
            assert buildinfo._from_git() == ""
            assert buildinfo.short()
        finally:
            buildinfo._from_git.cache_clear()

    def test_it_is_read_once_rather_than_every_repaint(self):
        buildinfo._from_git.cache_clear()
        first = buildinfo._from_git()
        assert buildinfo._from_git() is first
