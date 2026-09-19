"""Layout, readability and mid-scan switching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import contextlib
import inspect

import pytest
from PySide6.QtWidgets import QComboBox
from models import NonJobRouting

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

import rulesets  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402
from flowlayout import FlowLayout, Spacer  # noqa: E402
from gui import MainWindow, TriageTableModel  # noqa: E402
from models import Category, Classification, EmailMessage, FolderPlan, TriageItem  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def make_item(uid="1", **overrides):
    kwargs = overrides.pop("classification", {})
    classification = Classification(**{
        "summary": "A recruiter invited you to interview. Book a slot this week.",
        "reasoning": "Calendly link and an explicit invitation.",
        "is_job_related": True, "category": Category.INTERVIEW,
        "confidence_score": 0.98, **kwargs,
    })
    message = EmailMessage(
        uid=uid, subject="Interview invitation", sender_name="Dana Reyes",
        sender_email="d@x.com", date=datetime.now(timezone.utc),
    )
    return TriageItem(message, classification, FolderPlan(), **overrides)


# ==========================================================================
# Nothing may run off the edge at any window width
# ==========================================================================
class TestFlowLayout:
    def build(self, qapp, labels, width=None):
        """A shown widget: an unshown one never runs its layout, so the
        geometry checks below would pass against stale default rectangles."""
        holder = QWidget()
        layout = FlowLayout(holder)
        for text in labels:
            layout.addWidget(QPushButton(text))
        holder.show()
        if width is not None:
            holder.resize(width, layout.heightForWidth(width))
            qapp.processEvents()
            layout.setGeometry(holder.rect())
            qapp.processEvents()
        return holder, layout

    @pytest.mark.parametrize("width", [1600, 1200, 900, 700, 560, 420])
    def test_no_widget_ever_extends_past_the_edge(self, qapp, width):
        holder, layout = self.build(qapp, [
            "Past 24 Hours", "Past 3 Days", "Past 7 Days", "Custom Range",
            "⚙︎ Gemini Flash-Lite (latest)", "Stop All", "Scan & Analyze",
            "Apply 12 Approved Folder Moves",
        ], width=width)
        for index in range(layout.count()):
            geometry = layout.itemAt(index).geometry()
            assert geometry.right() <= width, f"widget {index} runs past {width}px"
            assert geometry.left() >= 0

    def test_no_two_widgets_overlap(self, qapp):
        holder, layout = self.build(qapp, [f"Button {i}" for i in range(9)], width=700)
        boxes = [layout.itemAt(i).geometry() for i in range(layout.count())]
        for i, first in enumerate(boxes):
            for second in boxes[i + 1:]:
                assert not first.intersects(second), "two widgets overlap"

    def test_it_wraps_rather_than_squeezing(self, qapp):
        holder, layout = self.build(qapp, [f"A fairly wide button {i}" for i in range(6)])
        wide = layout.heightForWidth(2000)
        narrow = layout.heightForWidth(400)
        assert narrow > wide, "the layout should gain rows as it narrows"

    def test_reported_height_matches_what_it_draws(self, qapp):
        holder, layout = self.build(qapp, [f"Button {i}" for i in range(7)], width=600)
        lowest = max(layout.itemAt(i).geometry().bottom() for i in range(layout.count()))
        assert lowest <= layout.heightForWidth(600) + 1

    def test_hidden_widgets_take_no_space(self, qapp):
        holder, layout = self.build(qapp, ["one", "two", "three"])
        full = layout.heightForWidth(200)
        layout.itemAt(1).widget().hide()
        assert layout.heightForWidth(200) <= full

    def test_a_spacer_collapses_instead_of_forcing_overflow(self, qapp):
        holder = QWidget()
        layout = FlowLayout(holder)
        layout.addWidget(QPushButton("left"))
        layout.addWidget(Spacer(16))
        layout.addWidget(QPushButton("right"))
        holder.show()
        holder.resize(300, layout.heightForWidth(300))
        qapp.processEvents()
        layout.setGeometry(holder.rect())
        qapp.processEvents()
        for index in range(layout.count()):
            assert layout.itemAt(index).geometry().right() <= 300


@contextlib.contextmanager
def painted_as(qapp, **kwargs):
    """Apply a theme for the body of a test, then put back what was there.

    ``theme.apply`` sets the application's palette, font and stylesheet.
    Restoring it by applying "light"/"normal" afterwards assumes that is
    what the session was using, and a test that guesses wrong leaves every
    later test measuring a different font. Whatever was there is saved and
    put back.
    """
    import theme

    was = (qapp.font(), qapp.palette(), qapp.styleSheet())
    try:
        yield theme.apply(qapp, **kwargs)
    finally:
        qapp.setFont(was[0])
        qapp.setPalette(was[1])
        qapp.setStyleSheet(was[2])


class TestWindowAtEverySize:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        window.model.set_items([make_item(str(i)) for i in range(6)])
        window.show()
        yield window
        window.close()

    @pytest.mark.parametrize("size", [(1600, 1000), (1200, 800), (900, 650), (760, 520)])
    def test_the_toolbars_never_clip(self, qapp, window, size):
        window.resize(*size)
        qapp.processEvents()
        for button in (window.scan_button, window.apply_button,
                       window.model_button):
            geometry = button.geometry()
            parent = button.parentWidget()
            assert geometry.right() <= parent.width() + 1, (
                f"{button.text()!r} runs past the edge at {size}"
            )
            assert button.width() >= button.sizeHint().width() - 1, (
                f"{button.text()!r} was squeezed below its natural width at {size}"
            )

    def test_the_window_can_be_made_small(self, qapp, window):
        assert window.minimumWidth() <= 800

    @pytest.mark.parametrize("size", [(1600, 1000), (820, 560)])
    def test_the_status_bar_never_pushes_the_window_wider(self, qapp, window, size):
        window.resize(*size)
        qapp.processEvents()
        window._set_status(
            "142 messages  ·  87 job-related  ·  61 ready to file  ·  26 need review  "
            "·  55 left in place  ·  61 selected  ·  and then some more text"
        )
        qapp.processEvents()
        assert window.status_label.width() <= size[0]
        assert window.statusBar().sizeHint().width() <= size[0] + 40

    def test_the_full_status_survives_in_the_tooltip(self, qapp, window):
        window.resize(820, 560)
        qapp.processEvents()
        message = "A status line far too long to fit in a narrow window " * 3
        window._set_status(message)
        assert window.status_label.toolTip() == message

    def test_resizing_re_elides_the_status(self, qapp, window):
        message = "Another quite long status message that will need to be shortened " * 2
        window.resize(1600, 900); qapp.processEvents(); window._set_status(message)
        wide = window.status_label.text()
        window.resize(800, 600); qapp.processEvents()
        assert window.status_label.text() != wide or "…" in window.status_label.text()


# ==========================================================================
# Table readability
# ==========================================================================
class TestReadability:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        yield window
        window.close()

    def test_rows_are_tall_enough_for_several_lines(self, window):
        height = window.table.verticalHeader().defaultSectionSize()
        assert height > 40, "three-line rows need more than a single line of height"

    def test_density_can_be_changed(self, window):
        window._set_density(1)
        compact = window.table.verticalHeader().defaultSectionSize()
        window._set_density(5)
        roomy = window.table.verticalHeader().defaultSectionSize()
        assert roomy > compact * 2

    def test_the_density_choice_is_remembered(self, window, tmp_path):
        window._set_density(2)
        assert Settings.load().row_lines == 2

    def test_the_wrapping_columns_use_the_wrap_delegate(self, window):
        from gui import WrapDelegate

        for column in (TriageTableModel.COL_SENDER, TriageTableModel.COL_SUBJECT,
                       TriageTableModel.COL_SUMMARY, TriageTableModel.COL_REASONING):
            assert isinstance(window.table.itemDelegateForColumn(column), WrapDelegate)

    def test_the_last_column_does_not_stretch(self, window):
        """Stretching the last section makes every other resize feel wrong."""
        assert window.table.horizontalHeader().stretchLastSection() is False

    def test_only_the_summary_stretches(self, window):
        from PySide6.QtWidgets import QHeaderView

        header = window.table.horizontalHeader()
        stretching = [
            column for column in range(window.model.columnCount())
            if header.sectionResizeMode(column) == QHeaderView.ResizeMode.Stretch
        ]
        assert stretching == [TriageTableModel.COL_SUMMARY]

    def test_columns_have_a_sensible_minimum(self, window):
        assert window.table.horizontalHeader().minimumSectionSize() >= 40

    def test_resetting_restores_the_defaults(self, window):
        window.table.setColumnWidth(TriageTableModel.COL_SENDER, 12)
        window._reset_columns()
        assert window.table.columnWidth(TriageTableModel.COL_SENDER) == \
            MainWindow.COLUMN_WIDTHS[TriageTableModel.COL_SENDER]


# ==========================================================================
# Switching backend and rule set while a scan runs
# ==========================================================================
class TestLiveSwitching:
    def test_the_engine_can_change_backend_mid_flight(self):
        from llm_engine import LLMEngine

        engine = LLMEngine(api_key="k", provider="gemini", batch_size=6)
        assert engine.batch_size == 6 and engine.usage.on_device is False
        engine.swap_provider("rules", ruleset="software")
        assert engine.provider.name == "rules"
        assert engine.usage.on_device is True
        assert engine.batch_size == 1, "local backends must drop to one email per request"
        assert engine.fallback_to_rules is False, "the rule set cannot fall back to itself"

    def test_a_swap_closes_the_old_backend(self):
        from llm_engine import LLMEngine

        engine = LLMEngine(api_key="k", provider="gemini")
        old = engine.provider
        engine.swap_provider("rules")
        assert old is not engine.provider
        assert old._session._closed is True

    def test_a_closed_engine_refuses_to_swap(self):
        from llm_engine import ClassificationCancelled, LLMEngine

        engine = LLMEngine(api_key="k", provider="gemini")
        engine.close()
        with pytest.raises(ClassificationCancelled):
            engine.swap_provider("rules")

    def test_the_rule_set_can_be_changed_in_place(self):
        from llm_engine import LLMEngine

        engine = LLMEngine(provider="rules", ruleset="general")
        assert engine.provider.classifier().ruleset.name == "general"
        engine.set_ruleset("academia")
        assert engine.provider.classifier().ruleset.name == "academia"

    def test_the_window_switches_a_running_scan(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        from llm_engine import LLMEngine
        from workers import ScanWorker

        window = MainWindow(Settings(icloud_email="a@b.com", provider="gemini"),
                            InMemoryCredentialStore())
        worker = ScanWorker(settings=window.settings, mailbox_password="p", api_key="k",
                            window_start=None, window_end=None)
        worker._classifier = LLMEngine(api_key="k", provider="gemini")
        try:
            assert worker.switch_model("rules", "rules-v1", ruleset="software") is True
            assert worker._classifier.provider.name == "rules"
            assert worker.switch_ruleset("healthcare") is True
            assert worker._classifier.ruleset == "healthcare"
        finally:
            worker._classifier.close()
            window.close()

    def test_switching_before_the_engine_exists_still_takes_effect(self, qapp):
        """It used to return False and change nothing; now it updates the
        settings the scan is about to build its engine from."""
        from workers import ScanWorker

        worker = ScanWorker(settings=Settings(), mailbox_password="", api_key="",
                            window_start=None, window_end=None)
        assert worker.switch_model("rules", "rules-v1") is True
        assert worker.settings.provider == "rules"
        assert worker.switch_ruleset("legal") is True
        assert worker.settings.ruleset == "legal"


# ==========================================================================
# Field rule sets
# ==========================================================================
class TestRuleSets:
    def test_every_ruleset_is_named_and_described(self):
        for name, label, blurb in rulesets.choices():
            assert name and label and blurb

    def test_general_is_the_default_and_adds_nothing(self):
        assert rulesets.DEFAULT_RULESET == "general"
        assert rulesets.get("general").signal_count == 0

    def test_an_unknown_name_falls_back_to_general(self):
        assert rulesets.get("underwater-basket-weaving").name == "general"

    @pytest.mark.parametrize("name", [r.name for r in rulesets.ALL_RULESETS if r.name != "general"])
    def test_each_field_adds_real_vocabulary(self, name):
        assert rulesets.get(name).signal_count >= 15

    @pytest.mark.parametrize(
        "name,subject,body,expected",
        [
            ("software", "Next step", "The next step is a system design interview.",
             Category.INTERVIEW),
            ("healthcare", "Next steps",
             "Please complete the credentialing packet and licensure verification.",
             Category.NEXT_STEPS),
            ("academia", "Invitation",
             "The search committee would like to invite you for a campus visit and job talk.",
             Category.INTERVIEW),
            ("government", "Status update",
             "You were not referred as you were not among the best qualified.",
             Category.NOT_INTERESTED),
            ("creative", "Next step", "We would like you to complete a design exercise.",
             Category.NEXT_STEPS),
            ("sales", "Congratulations",
             "We are pleased to offer you the role with on target earnings of $180k.",
             Category.OFFER),
        ],
    )
    def test_field_vocabulary_lands_the_verdict(self, name, subject, body, expected):
        from rules_engine import RuleClassifier

        verdict = RuleClassifier(ruleset=name).classify(
            subject=subject, body=body, sender="hr@example.com"
        )
        assert verdict.is_job_related
        assert verdict.category is expected

    def test_overlays_only_add(self):
        """Picking the wrong field costs recall, never a wrong confident answer."""
        from rules_engine import RuleClassifier

        base = RuleClassifier()
        for ruleset in rulesets.ALL_RULESETS:
            assert RuleClassifier(ruleset=ruleset.name).signal_count >= base.signal_count

    def test_a_shared_rejection_reads_the_same_in_every_field(self):
        from rules_engine import RuleClassifier

        body = "We have decided to move forward with other candidates."
        for ruleset in rulesets.ALL_RULESETS:
            verdict = RuleClassifier(ruleset=ruleset.name).classify(
                subject="Update", body=body, sender="c@x.com"
            )
            assert verdict.category is Category.NOT_INTERESTED


class TestSwitchingBeforeTheEngineExists:
    """A scan spends its first seconds in IMAP, before the classifier is built.
    Switching then used to claim success and change nothing."""

    def worker(self, qapp):
        from workers import ScanWorker

        return ScanWorker(settings=Settings(icloud_email="a@b.com", provider="gemini"),
                          mailbox_password="p", api_key="k",
                          window_start=None, window_end=None)

    def test_it_updates_the_settings_the_scan_will_use(self, qapp):
        worker = self.worker(qapp)
        assert worker._classifier is None
        assert worker.switch_model("rules", "rules-v1", ruleset="legal") is True
        assert worker.settings.provider == "rules"
        assert worker.settings.model == "rules-v1"
        assert worker.settings.ruleset == "legal"

    def test_a_ruleset_change_lands_too(self, qapp):
        worker = self.worker(qapp)
        assert worker.switch_ruleset("trades") is True
        assert worker.settings.ruleset == "trades"

    def test_the_window_reports_honestly_either_way(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        from workers import ScanWorker

        window = MainWindow(Settings(icloud_email="a@b.com", provider="gemini"),
                            InMemoryCredentialStore())
        try:
            class Pending(ScanWorker):
                def isRunning(self):  # noqa: N802
                    return True

                def switch_model(self, **kwargs):
                    return False        # nothing to swap yet

            window.scan_worker = Pending(
                settings=window.settings, mailbox_password="p", api_key="k",
                window_start=None, window_end=None,
            )
            window._switch_model("rules", "rules-v1")
            assert "next scan" in window.status_label.toolTip()
        finally:
            window.scan_worker = None
            window.close()


class TestTheStatusLineStaysInsideItsLabel:
    """"The 'analyzed' status bar in the main screen's text goes out of
    bounds."

    It was cut to fit once, when it was set. The width it was cut to is
    decided by the layout, and the usage figure to its right grows while a
    run is going - so a message that fitted when it was set had some of its
    room taken away afterwards and ran off the end.
    """

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"),
                            InMemoryCredentialStore())
        window.model.set_items([make_item(str(i)) for i in range(6)])
        window.show()
        yield window
        window.close()

    def test_the_text_fits_the_label_it_is_in(self, qapp, window):
        from PySide6.QtGui import QFontMetrics

        window.resize(900, 600)
        qapp.processEvents()
        window._set_status("Analyzed 128 of 512 fetched so far…")
        qapp.processEvents()
        label = window.status_label
        width = QFontMetrics(label.font()).horizontalAdvance(label.text())
        assert width <= label.width(), (
            f"the status text is {width}px wide in a {label.width()}px label")

    def test_the_progress_bars_own_text_fits_the_bar(self, qapp, window,
                                                     monkeypatch):
        """Where it actually ran out of bounds.

        Qt centres the text it draws inside a progress bar and lets it run
        out past the widget. The bar is never wider than 320 pixels and the
        message is twice that.
        """
        from PySide6.QtGui import QFontMetrics

        window.resize(900, 600)
        qapp.processEvents()
        monkeypatch.setattr(window, "running_workers", lambda: True)
        message = "Analyzed 128 of 512 fetched so far, and a good way to go"
        window._on_progress(128, 512, message)
        qapp.processEvents()
        bar = window.progress
        shown = (bar.format().replace("%v", str(bar.value()))
                 .replace("%m", str(bar.maximum())))
        width = QFontMetrics(bar.font()).horizontalAdvance(shown)
        assert width <= bar.width(), (
            f"the bar draws {width}px of text in a {bar.width()}px bar: "
            f"{shown!r}")
        assert bar.toolTip() == message, "the whole message was lost"

    def test_the_progress_bar_still_says_where_it_is_up_to(self, qapp, window,
                                                           monkeypatch):
        """Cutting it to fit must not cut the counts off."""
        monkeypatch.setattr(window, "running_workers", lambda: True)
        window.resize(900, 600)
        qapp.processEvents()
        window._on_progress(128, 512, "Analyzed 128 of 512 fetched so far")
        assert "%v / %m" in window.progress.format(), (
            f"the bar no longer shows its counts: "
            f"{window.progress.format()!r}")


class TestTheDropdownsShowTheirOptions:
    """"Options in dropdowns in rules settings are cut off as well. When
    longer entries are selected they do not fully display in their boxes."

    Qt sizes a combo's menu to the combo, and these are deliberately narrow:
    three or four of them divide one row. So an option too long for the box
    was cut short in the menu as well, where there is nothing beside it and
    no reason to cut it.
    """

    @staticmethod
    def _combo(qapp, options):
        from widgets import RoomyCombo

        combo = RoomyCombo()
        for option in options:
            combo.addItem(option)
        combo.setMinimumWidth(88)
        combo.resize(88, 26)
        combo.show()
        qapp.processEvents()
        return combo

    @staticmethod
    def _done(combo):
        """Closed and handed to Qt to delete, so the session's widget
        reaper does not find it later."""
        combo.hidePopup()
        combo.close()
        combo.deleteLater()

    OPTIONS = ("is", "is not", "contains", "does not contain",
               "matches this regular expression", "is one of my mailboxes")

    def test_the_menu_is_as_wide_as_the_longest_option(self, qapp):
        from PySide6.QtGui import QFontMetrics

        combo = self._combo(qapp, self.OPTIONS)
        try:
            combo.showPopup()
            qapp.processEvents()
            metrics = QFontMetrics(combo.font())
            widest = max(metrics.horizontalAdvance(option)
                         for option in self.OPTIONS)
            room = combo.view().minimumWidth()
            assert room >= widest, (
                f"the longest option needs {widest}px and the menu offers "
                f"{room}px, so it is cut off")
        finally:
            self._done(combo)

    def test_the_box_asks_for_room_for_what_it_is_showing(self, qapp):
        from PySide6.QtGui import QFontMetrics

        combo = self._combo(qapp, self.OPTIONS)
        try:
            combo.setCurrentIndex(
                self.OPTIONS.index("matches this regular expression"))
            qapp.processEvents()
            needed = QFontMetrics(combo.font()).horizontalAdvance(
                combo.currentText())
            assert combo.minimumWidth() >= min(combo.MOST, needed), (
                f"showing {combo.currentText()!r} needs {needed}px and the "
                f"box only asks for {combo.minimumWidth()}px")
        finally:
            self._done(combo)

    def test_a_very_long_entry_still_gives_way(self, qapp):
        """A box that insists on its text would push the dialog off the
        screen."""
        combo = self._combo(qapp, ("short", "x" * 400))
        try:
            combo.setCurrentIndex(1)
            qapp.processEvents()
            assert combo.minimumWidth() <= combo.MOST, (
                f"the box demands {combo.minimumWidth()}px of the layout")
        finally:
            self._done(combo)

    def test_a_short_option_does_not_shrink_the_box(self, qapp):
        """The floor the dialog asked for is still the floor."""
        combo = self._combo(qapp, self.OPTIONS)
        try:
            combo.setCurrentIndex(0)      # "is"
            qapp.processEvents()
            assert combo.minimumWidth() >= 88, (
                f"the box shrank to {combo.minimumWidth()}px, under the 88 "
                f"the row asked for")
        finally:
            self._done(combo)

    def test_the_rules_dialog_uses_them(self, qapp):
        """Where the report came from."""
        import settings_dialog
        from widgets import RoomyCombo

        source = inspect.getsource(settings_dialog)
        assert "QComboBox()" not in source, (
            "a plain QComboBox is still being built in the settings dialog")
        assert issubclass(RoomyCombo, QComboBox)


class TestMaximumContrastReachesTheRows:
    """"Max contrast maintains the same level of contrast for text in
    unselected rows in the viewer."

    It did. Maximum contrast is monochrome on purpose, and the palette says
    so, but the table's row colours were written out as fixed values: a grey
    for a row already filed, another for a folder being left alone, a hue
    for the category. None of them consult the palette, so every unselected
    row read exactly as it did at normal contrast.
    """

    @staticmethod
    def _item(moved=False, leave=False):
        from tests.test_gui import make_item

        item = make_item("1")
        item.moved = moved
        if leave:
            item.non_job_routing = NonJobRouting.LEAVE
        return item

    def _foreground(self, qapp, contrast, column):
        """What the model says to paint a row's text in."""
        from triage_table import TriageTableModel

        with painted_as(qapp, mode="light", contrast=contrast):
            model = TriageTableModel()
            model.set_items([self._item(moved=True)])
            index = model.index(0, column)
            return model.data(index, Qt.ItemDataRole.ForegroundRole)

    def test_a_filed_rows_text_follows_the_palette(self, qapp):
        from triage_table import TriageTableModel

        normal = self._foreground(qapp, "normal", TriageTableModel.COL_SUBJECT)
        most = self._foreground(qapp, "maximum", TriageTableModel.COL_SUBJECT)
        assert normal is not None, (
            "a filed row used to be painted grey, and this no longer sees it")
        assert most is None, (
            f"at maximum contrast the row is still painted {most.name()}, "
            f"which is the same grey as at normal contrast")

    def test_the_monochrome_flag_follows_what_was_applied(self, qapp):
        import theme

        with painted_as(qapp, mode="light", contrast="maximum"):
            assert theme.monochrome() is True
            assert theme.active_contrast() == "maximum"
        with painted_as(qapp, mode="light", contrast="high"):
            assert theme.monochrome() is False
        with painted_as(qapp, mode="light", contrast="normal"):
            assert theme.monochrome() is False

    def test_the_category_chip_gives_up_its_hue(self, qapp):
        """The dot still carries the colour; the words do not."""
        import theme
        from PySide6.QtGui import QPalette

        with painted_as(qapp, mode="light", contrast="maximum"):
            ink = qapp.palette().color(QPalette.ColorRole.Text)
            assert theme.monochrome()
            assert ink.name() == "#000000", (
                f"maximum contrast in light mode writes in {ink.name()}")


class TestNothingTouchesTheSplitterBar:
    """"The attachments button and the field below it are too close to the
    separating bar to their right. Ensure proper padding across the app."

    Both halves of the preview had zero margins, so everything down the
    right edge of the left half sat hard against the handle between them.
    """

    #: Written out rather than read from PreviewPane.GUTTER. Taken from the
    #: constant, every assertion below slides with it: setting it to zero
    #: made the whole class pass against the layout it was written to
    #: reject.
    LEAST = 6

    @pytest.fixture
    def pane(self, qapp):
        from triage_table import PreviewPane

        pane = PreviewPane()
        pane.resize(1000, 600)
        pane.show()
        qapp.processEvents()
        yield pane
        pane.close()
        pane.deleteLater()

    def test_the_attachments_button_clears_the_bar(self, qapp, pane):
        handle = pane.splitter.handle(1)
        button = pane.attachments_button
        right = button.mapTo(pane, button.rect().topRight()).x()
        bar = handle.mapTo(pane, handle.rect().topLeft()).x()
        assert bar - right >= self.LEAST, (
            f"the button's right edge is at {right} and the bar starts at "
            f"{bar}, a gap of {bar - right}px")

    def test_the_text_box_clears_the_bar(self, qapp, pane):
        handle = pane.splitter.handle(1)
        view = pane.body_view
        right = view.mapTo(pane, view.rect().topRight()).x()
        bar = handle.mapTo(pane, handle.rect().topLeft()).x()
        assert bar - right >= self.LEAST, (
            f"the text box's right edge is at {right} and the bar starts at "
            f"{bar}, a gap of {bar - right}px")

    def test_the_analysis_side_clears_it_too(self, qapp, pane):
        handle = pane.splitter.handle(1)
        view = pane.reasoning_view
        left = view.mapTo(pane, view.rect().topLeft()).x()
        bar = handle.mapTo(pane, handle.rect().topRight()).x()
        assert left - bar >= self.LEAST, (
            f"the reasoning box starts at {left} and the bar ends at {bar}, "
            f"a gap of {left - bar}px")
