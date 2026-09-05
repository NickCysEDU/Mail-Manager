"""The menu bar item: what it offers and what it reports back."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import providers  # noqa: E402
import rulesets  # noqa: E402
from menubar import MenuBarController  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(qapp):
    """A controller with a menu but no tray icon, which needs no menu bar."""
    subject = MenuBarController()
    subject._menu = QMenu()
    yield subject
    subject._menu = None


def titles(menu):
    return [action.text() for action in menu.actions()]


def submenu(menu, prefix):
    for action in menu.actions():
        if action.text().startswith(prefix) and action.menu() is not None:
            return action.menu()
    raise AssertionError(f"no submenu starting with {prefix!r} in {titles(menu)}")


def test_the_model_menu_is_named_after_the_current_model(controller):
    """The menu bar should answer "what will this use?" without being opened."""
    controller.set_model("rules", "rules-v1", "general")
    controller.rebuild(0)
    label = providers.provider_class("rules").models[0].label
    assert any(t == f"Model: {label}" for t in titles(controller._menu))


def test_every_backend_and_model_is_offered(controller):
    controller.set_model("rules", "rules-v1", "general")
    controller.rebuild(0)
    expected = sum(len(providers.provider_class(name).models)
                   for name, _label, _blurb in providers.provider_choices())
    assert len(controller._model_actions) == expected


def test_the_current_model_is_the_checked_one(controller):
    controller.set_model("rules", "rules-v1", "software")
    controller.rebuild(0)
    checked = [key for key, action in controller._model_actions.items()
               if action.isChecked()]
    assert checked == [("rules", "rules-v1")]
    assert [name for name, action in controller._ruleset_actions.items()
            if action.isChecked()] == ["software"]


def test_choosing_a_model_reports_the_provider_and_the_model(controller):
    controller.set_model("rules", "rules-v1", "general")
    controller.rebuild(0)
    seen = []
    controller.modelChanged.connect(lambda p, m: seen.append((p, m)))
    target = ("rules", "rules-v1")
    for key, action in controller._model_actions.items():
        if key != target:
            target = key
            action.trigger()
            break
    assert seen == [target]


def test_choosing_a_rule_set_reports_it(controller):
    controller.set_model("rules", "rules-v1", "general")
    controller.rebuild(0)
    seen = []
    controller.rulesetChanged.connect(seen.append)
    name = [n for n, _l, _b in rulesets.choices() if n != "general"][0]
    controller._ruleset_actions[name].trigger()
    assert seen == [name]


def test_following_a_change_made_in_the_window(controller):
    """Switching in the main window has to move the menu bar's check mark."""
    controller.set_model("rules", "rules-v1", "general")
    controller.rebuild(0)
    controller.set_model("rules", "rules-v1", "healthcare")
    assert [n for n, a in controller._ruleset_actions.items() if a.isChecked()] \
        == ["healthcare"]


def test_an_unknown_backend_does_not_break_the_title(controller):
    controller.set_model("nonesuch", "whatever", "general")
    controller.rebuild(0)
    assert any(t.startswith("Model:") for t in titles(controller._menu))


def test_the_schedule_survives_a_model_change(controller):
    """set_model rebuilds the menu, which must not silently clear the interval."""
    controller.set_model("rules", "rules-v1", "general")
    controller.rebuild(60)
    assert [m for m, a in controller._interval_actions.items() if a.isChecked()] == [60]
    controller.set_model("rules", "rules-v1", "legal")
    assert [m for m, a in controller._interval_actions.items() if a.isChecked()] == [60]
