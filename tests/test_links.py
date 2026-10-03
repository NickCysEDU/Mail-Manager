"""Links from mail: listed by where they go, and said before they open."""

from __future__ import annotations

import pytest

from tests.conftest import make_item

LINKS = ("https://boards.example.com/jobs/4521/apply?src=mail",
         "https://calendly.example.com/talent/30min",
         "mailto:recruiting@example.com")


class TestWhereALinkGoes:
    @pytest.mark.parametrize("url, host, rest", [
        ("https://boards.example.com/jobs/1?x=2", "boards.example.com",
         "/jobs/1?x=2"),
        ("http://Example.COM", "example.com", ""),
        ("mailto:someone@example.com", "someone@example.com", ""),
        # The words before an @ are not where it goes.
        ("https://good.example.com@bad.example.net/x", "bad.example.net",
         "/x"),
    ])
    def test_the_site_and_the_rest(self, url, host, rest):
        import link_open

        assert link_open.parts(url) == (host, rest)

    @pytest.mark.parametrize("url", [
        "javascript:alert(1)", "file:///etc/passwd", "data:text/html,hi",
        "ftp://example.com/x", "", "not a link", "https://", "mailto:"])
    def test_anything_else_is_not_opened(self, url):
        import link_open

        assert link_open.parts(url) is None and not link_open.opens(url)

    def test_a_name_in_another_alphabet_is_spelled_out(self):
        import link_open

        # A Cyrillic a, which looks like a Latin one.
        shown = link_open.shown_host("аpple.example.com")
        assert "xn--" in shown
        assert link_open.shown_host("apple.example.com") == "apple.example.com"


class TestOpeningOne:
    @pytest.fixture
    def opened(self, monkeypatch):
        from PySide6.QtGui import QDesktopServices

        seen = []
        monkeypatch.setattr(QDesktopServices, "openUrl",
                            lambda url: seen.append(url.toString()) or True)
        return seen

    def test_it_asks_first(self, qapp, opened):
        import link_open

        asked = []

        def ask(url):
            asked.append(url)
            return False, False

        assert link_open.open_link(LINKS[0], ask=ask) == (False, False)
        assert asked == [LINKS[0]] and opened == []

    def test_it_opens_when_told_to(self, qapp, opened):
        import link_open

        result = link_open.open_link(LINKS[0], ask=lambda url: (True, False))
        assert result == (True, False) and opened == [LINKS[0]]

    def test_dont_show_this_again_is_passed_back(self, qapp, opened):
        import link_open

        assert link_open.open_link(LINKS[1], ask=lambda url: (True, True)) == \
            (True, True)

    def test_without_the_warning_it_opens_at_once(self, qapp, opened):
        import link_open

        def ask(url):
            raise AssertionError("asked although told not to")

        assert link_open.open_link(LINKS[0], warn=False, ask=ask)[0]
        assert opened == [LINKS[0]]

    def test_a_link_of_another_kind_never_opens(self, qapp, opened):
        import link_open

        result = link_open.open_link("file:///etc/passwd", warn=False)
        assert result == (False, False) and opened == []

    def test_the_warning_says_where_and_offers_to_stop_asking(self, qtbot):
        from PySide6.QtWidgets import QLabel

        import link_open

        dialog = link_open.LinkWarning(LINKS[0])
        qtbot.addWidget(dialog)
        words = [label.text() for label in dialog.findChildren(QLabel)]
        assert "boards.example.com" in words and LINKS[0] in words
        assert dialog.never.text() == "Don't show this again"
        assert not dialog.never.isChecked()


class TestTheList:
    def test_it_lists_what_can_be_opened(self, qtbot):
        import link_open

        dialog = link_open.LinkList(list(LINKS) + ["javascript:void(0)"])
        qtbot.addWidget(dialog)
        assert dialog.list.count() == 3

    def test_open_and_copy_use_the_one_chosen(self, qtbot):
        from PySide6.QtGui import QGuiApplication

        import link_open

        dialog = link_open.LinkList(LINKS)
        qtbot.addWidget(dialog)
        chosen = []
        dialog.chosen.connect(chosen.append)
        dialog.list.setCurrentRow(1)
        dialog.open_button.click()
        assert chosen == [LINKS[1]]
        dialog.copy_button.click()
        assert QGuiApplication.clipboard().text() == LINKS[1]

    def test_with_nothing_to_open_nothing_is_offered(self, qtbot):
        import link_open

        dialog = link_open.LinkList(["javascript:void(0)"])
        qtbot.addWidget(dialog)
        assert not dialog.open_button.isEnabled()
        assert not dialog.copy_button.isEnabled()


class TestTheMessageView:
    @staticmethod
    def _pane(qtbot, links=LINKS):
        from triage_table import PreviewPane

        pane = PreviewPane()
        qtbot.addWidget(pane)
        pane.show_item(0, make_item(email_kwargs={"links": tuple(links)}))
        return pane

    def test_the_button_says_how_many(self, qtbot):
        pane = self._pane(qtbot)
        assert pane.links_button.isEnabled()
        assert pane.links_button.text() == "Links (3)"
        none = self._pane(qtbot, links=())
        assert not none.links_button.isEnabled()
        assert none.links_button.text() == "Links"

    def test_the_list_asks_the_window_to_open_one(self, qtbot):
        pane = self._pane(qtbot)
        asked = []
        pane.linkRequested.connect(asked.append)
        pane._show_links()
        dialog = pane._link_list
        dialog.list.setCurrentRow(0)
        dialog.open_button.click()
        dialog.close()
        assert asked == [LINKS[0]]

    def test_the_analysis_does_not_open_links_itself(self, qtbot):
        from PySide6.QtCore import QUrl

        pane = self._pane(qtbot)
        view = pane.reasoning_view
        assert not view.openExternalLinks() and not view.openLinks()
        asked = []
        pane.linkRequested.connect(asked.append)
        view.anchorClicked.emit(QUrl(LINKS[1]))
        assert asked == [LINKS[1]]


class TestTheSetting:
    def test_it_is_on_until_turned_off(self):
        import config

        assert config.Settings().warn_on_links is True
        settings = config.Settings.from_dict({"warn_on_links": False})
        assert settings.warn_on_links is False
        assert config.Settings.from_dict(settings.to_dict()).warn_on_links \
            is False

    def test_the_window_stops_asking_when_told(self, qapp, monkeypatch):
        import gui
        import link_open

        class Window:
            class settings:
                warn_on_links = True
                saved = 0

                @classmethod
                def save(cls):
                    cls.saved += 1

        asked = []

        def open_link(url, parent=None, warn=True, ask=None):
            asked.append(warn)
            return True, True

        monkeypatch.setattr(link_open, "open_link", open_link)
        window = Window()
        gui.MainWindow._open_link(window, LINKS[0])
        assert asked == [True]
        assert Window.settings.warn_on_links is False
        assert Window.settings.saved == 1
        gui.MainWindow._open_link(window, LINKS[0])
        assert asked == [True, False]

    def test_settings_shows_it_and_keeps_it(self, qtbot):
        import config
        from settings_dialog import SettingsDialog

        settings = config.Settings(warn_on_links=False)
        dialog = SettingsDialog(settings, config.InMemoryCredentialStore())
        qtbot.addWidget(dialog)
        assert not dialog.links_check.isChecked()
        dialog.links_check.setChecked(True)
        assert dialog.collect().warn_on_links is True
