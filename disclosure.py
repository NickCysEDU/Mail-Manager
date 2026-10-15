"""What a model elsewhere is sent, said once for each before it is sent
anything.

The built-in rules and a model on this Mac keep mail here; any other backend
is sent every message a scan reads. Choosing one in Settings, from the model
menu or while setting up first says what it will be sent, and goes ahead only
if the person says to. Each is asked about once.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

import providers
from widgets import _attr_url, _html


def needed(settings, provider: str, base_url: str = "") -> bool:
    """Whether choosing this backend has to be asked about first."""
    return (providers.leaves_this_mac(provider, base_url)
            and provider not in settings.told_what_is_sent)


def describe(provider: str) -> str:
    """What the backend is sent, in the dialog's words."""
    spec = providers.provider_class(provider)
    parts = [
        f"<p>For each message a scan reads, {_html(spec.label)} is sent who "
        "sent it and to whom, its subject and date, the names of its "
        "attachments, where its links go, and its text without quoted "
        "history, signatures and footers, trimmed when long. Codes, "
        "passwords, keys and the secret parts of links are taken out "
        "first.</p>",
        "<p>Attachments are never sent, and a message already sorted is not "
        "sent again. Scans in the background send mail the same way.</p>",
    ]
    if spec.privacy_note:
        terms = (f' <a href="{_attr_url(spec.privacy_url)}">Their terms</a>'
                 if spec.privacy_url else "")
        parts.append(f"<p>{_html(spec.privacy_note)}{terms}</p>")
    return "".join(parts)


def ask(parent, settings, provider: str, base_url: str = "") -> bool:
    """Say what the backend will be sent, if it sends mail away and has not
    been said already. True to go on with it, which is remembered in
    ``settings``."""
    if not needed(settings, provider, base_url):
        return True
    label = providers.provider_class(provider).label
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("What is sent")
    box.setText(f"{label} will be sent your mail")
    box.setInformativeText(describe(provider))
    use = box.addButton(f"Use {label}", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() is not use:
        return False
    settings.told_what_is_sent = [*settings.told_what_is_sent, provider]
    return True
