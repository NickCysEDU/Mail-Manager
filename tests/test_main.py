"""Entry point: argument parsing, logging setup and the runtime self-test."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

import main as main_module
from main import _bundled_path, _clean_argv, build_parser, configure_logging, self_test
from models import APP_VERSION


class TestArgumentParsing:
    def test_defaults(self):
        args = build_parser().parse_args([])
        assert args.log_level == "INFO"
        assert args.reset_settings is False
        assert args.print_paths is False
        assert args.self_test is False

    def test_version_exits_cleanly(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--version"])
        assert excinfo.value.code == 0
        assert APP_VERSION in capsys.readouterr().out

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
    def test_log_levels(self, level):
        assert build_parser().parse_args(["--log-level", level]).log_level == level

    def test_unknown_log_level_is_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--log-level", "TRACE"])


class TestCleanArgv:
    def test_strips_the_macos_process_serial_number(self):
        """Finder can pass -psn_0_12345, which argparse would reject."""
        assert _clean_argv(["-psn_0_123456", "--print-paths"]) == ["--print-paths"]

    def test_leaves_normal_arguments_alone(self):
        assert _clean_argv(["--log-level", "DEBUG"]) == ["--log-level", "DEBUG"]

    def test_falls_back_to_sys_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["app", "-psn_0_1", "--version"])
        assert _clean_argv(None) == ["--version"]

    def test_a_bundle_launch_parses(self):
        args = build_parser().parse_args(_clean_argv(["-psn_0_98765"]))
        assert args.print_paths is False


class TestLogging:
    def test_creates_a_log_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        path = configure_logging("INFO")
        logging.getLogger("test").info("hello")
        logging.shutdown()
        assert path.exists()
        assert "hello" in path.read_text()

    def test_level_is_applied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_http_libraries_are_quietened(self, tmp_path, monkeypatch):
        """Debug logging from httpx would put request URLs in the log file."""
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        configure_logging("DEBUG")
        for name in ("httpx", "httpcore", "anthropic", "httpx2"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_handlers_are_not_duplicated_across_calls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        configure_logging("INFO")
        first = len(logging.getLogger().handlers)
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == first


class TestSelfTest:
    def test_passes_in_a_healthy_environment(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        assert self_test() == 0
        output = capsys.readouterr().out
        assert "All checks passed." in output
        for label in ("PySide6", "Qt platform plugins", "anthropic SDK", "keyring backend"):
            assert label in output

    def test_reports_a_broken_dependency_without_raising(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setattr(
            main_module, "_qt_plugin_check",
            lambda: (_ for _ in ()).throw(RuntimeError("no platform plugins")),
        )
        assert self_test() == 1
        output = capsys.readouterr().out
        assert "FAILED" in output
        assert "Some checks FAILED." in output


class TestTheSelfTestHasTeeth:
    """Checks that fail, rather than remarks. The dial face was reported
    "NOT bundled" by every shipped build's self test, above "All checks
    passed" - which is what the build reads to decide whether to ship."""

    @pytest.mark.parametrize("missing", ["Michroma-Regular.ttf",
                                         "Michroma-OFL.txt"])
    def test_a_bundle_without_the_dial_face_or_its_licence_fails(
            self, tmp_path, monkeypatch, capsys, missing):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        real = main_module._bundled_path
        monkeypatch.setattr(
            main_module, "_bundled_path",
            lambda name: None if name.endswith(missing) else real(name))
        assert self_test() == 1
        output = capsys.readouterr().out
        line = next(row for row in output.splitlines() if "dial lettering" in row)
        assert "FAILED" in line and missing in line, line

    def test_bests_that_are_not_kept_fail(self, tmp_path, monkeypatch, capsys):
        import rider_bests

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setattr(rider_bests.Bests, "offer",
                            lambda self, track, mode, worth: (None, False))
        assert self_test() == 1
        line = next(row for row in capsys.readouterr().out.splitlines()
                    if "attachment viewer" in row)
        assert "FAILED" in line and "bests" in line, line


class TestTheSelfTestLooksForWhatIsImportedLate:
    """Modules the viewer imports inside a method, on the game's first
    frame or the first time the scope is drawn on the card - the kind a
    bundle can lose without anything else noticing."""

    @pytest.mark.parametrize("module,words", [("rider_layout", "road"),
                                              ("scope_gl", "scope"),
                                              ("av_sync", "beat")])
    def test_a_bundle_without_it_fails(self, tmp_path, monkeypatch, capsys,
                                       module, words):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        # What a bundle that lost it does on ``import``.
        monkeypatch.setitem(sys.modules, module, None)
        assert self_test() == 1
        line = next(row for row in capsys.readouterr().out.splitlines()
                    if "attachment viewer" in row)
        assert "FAILED" in line and module in line, line

    def test_and_says_so_when_it_is_there(self, tmp_path, monkeypatch,
                                          capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        self_test()
        line = next(row for row in capsys.readouterr().out.splitlines()
                    if "attachment viewer" in row)
        assert "the rider's road" in line and "the scope on the card" in line
        assert "the picture on the beat" in line


class TestBundledPath:
    def test_finds_a_resource_next_to_the_source(self):
        assert _bundled_path("assets/icon.png") is not None

    def test_missing_resource_returns_none(self):
        assert _bundled_path("assets/does-not-exist.png") is None

    def test_prefers_the_pyinstaller_extraction_directory(self, tmp_path, monkeypatch):
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "icon.png").write_bytes(b"x")
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        try:
            assert _bundled_path("assets/icon.png") == tmp_path / "assets" / "icon.png"
        finally:
            monkeypatch.delattr(sys, "_MEIPASS", raising=False)


class TestMainEntry:
    def test_print_paths(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        assert main_module.main(["--print-paths"]) == 0
        output = capsys.readouterr().out
        assert "settings:" in output and "logs:" in output and "keychain:" in output

    def test_self_test_flag(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        assert main_module.main(["--self-test"]) == 0
        assert "self test" in capsys.readouterr().out

    def test_reset_settings_removes_the_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        import config

        config.Settings(icloud_email="a@b.com").save()
        assert config.settings_path().exists()
        main_module.main(["--reset-settings", "--print-paths"])
        assert not config.settings_path().exists()

    def test_reset_settings_on_a_clean_install_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        assert main_module.main(["--reset-settings", "--print-paths"]) == 0


class TestTheSelfTestHearsAndCounts:
    def test_a_rider_that_cannot_hear_a_key_fails(self, tmp_path, monkeypatch,
                                                  capsys):
        import harmony

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setattr(harmony, "analyse", lambda *a, **k: None)
        assert self_test() == 1

    def test_a_rider_that_cannot_count_fails(self, tmp_path, monkeypatch,
                                             capsys):
        import trackstyle

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setattr(trackstyle, "rhythm_of", lambda *a, **k: None)
        assert self_test() == 1
