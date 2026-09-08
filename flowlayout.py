"""A layout that wraps its children onto new rows instead of overflowing.

Qt's QHBoxLayout squeezes widgets past their sizeHint and then clips them, so a
toolbar that fits at 1400 px pushes its buttons off the edge at 900 px. This
lays items left to right and starts a new row whenever the next one would not
fit, which means the action bar stays fully usable at any window width.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto additional rows as needed."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        spacing: int = 6,
        vertical_spacing: int = 6,
    ) -> None:
        super().__init__(parent)
        self._items: List[QLayoutItem] = []
        self._hspacing = spacing
        self._vspacing = vertical_spacing
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    # -- QLayout plumbing ------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setSpacing(self, spacing: int) -> None:  # noqa: N802
        """Space between items on a row."""
        self._hspacing = max(0, int(spacing))
        self.invalidate()

    def spacing(self) -> int:
        return self._hspacing

    def setVerticalSpacing(self, spacing: int) -> None:  # noqa: N802
        """Space between rows, when the toolbar has to wrap."""
        self._vspacing = max(0, int(spacing))
        self.invalidate()

    def verticalSpacing(self) -> int:  # noqa: N802
        return self._vspacing

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    # -- the actual algorithm --------------------------------------------
    def _layout(self, rect: QRect, apply: bool) -> int:
        """Pack items into rows, then centre each row on its own middle.

        Two passes rather than one. A row's height is not known until the last
        item has joined it, and placing items as they arrive pins every one of
        them to the top of the row - which leaves a short label like "to", or
        the window's date range, floating above the buttons beside it instead
        of sitting on the same line.
        """
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y = area.x(), area.y()
        row: list = []
        row_height = 0

        def flush(items, top: int, height: int) -> None:
            if not apply:
                return
            for placed, hint in items:
                offset = (height - hint.height()) // 2
                placed.setGeometry(QRect(QPoint(placed_x[placed], top + offset), hint))

        placed_x: dict = {}

        def close_row(items, top: int, height: int, used: int) -> None:
            """Hand a row's leftover width to whatever asked to grow.

            Without this a field is always exactly its size hint, which for a
            line edit is about seventeen characters whatever the window is
            doing - so its placeholder is clipped on a 2000 pixel display.
            """
            if not items:
                return
            greedy = [pair for pair in items
                      if pair[0].expandingDirections() & Qt.Orientation.Horizontal]
            if greedy:
                spare = area.width() - used
                if spare > 0:
                    share = spare // len(greedy)
                    shift = 0
                    for pair in items:
                        placed_x[pair[0]] += shift
                        if pair in greedy:
                            pair[1].setWidth(pair[1].width() + share)
                            shift += share
            flush(items, top, height)

        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = QSize(item.sizeHint())
            next_x = x + hint.width() + self._hspacing
            if next_x - self._hspacing > area.right() and row:
                close_row(row, y, row_height, x - area.x() - self._hspacing)
                row = []
                x = area.x()
                y = y + row_height + self._vspacing
                next_x = x + hint.width() + self._hspacing
                row_height = 0
            placed_x[item] = x
            row.append((item, hint))
            x = next_x
            row_height = max(row_height, hint.height())

        close_row(row, y, row_height, x - area.x() - self._hspacing)
        return y + row_height - rect.y() + margins.bottom()


class Spacer(QWidget):
    """A stretchy gap that collapses to nothing when a row has to wrap."""

    def __init__(self, minimum: int = 12, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(minimum)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(self.minimumWidth(), 1)
