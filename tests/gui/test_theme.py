"""Tests for the GUI theme's font handling (gui.theme).

The app pins a concrete font FAMILY at start-up, because the platform reports aliases like
"Sans Serif" that Qt re-resolves expensively. The size must survive that: it is where the
user's OS text-size setting lives, and overwriting it silently undoes an accessibility
setting somebody depends on to read the screen at all.
Used by:    pytest (the ``app`` fixture and the headless offscreen platform come from
            tests/gui/conftest.py).
Depends on: gui.theme, PySide6.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from gui.theme import _base_point_size, build_stylesheet, configure_application_font


def test_configure_application_font_preserves_the_users_text_size(app) -> None:
    # The regression this guards: app.setFont(QFont(family)) looks harmless but that
    # constructor carries Qt's built-in 12pt, so someone running 20pt system text for
    # readability silently got a 12pt app.
    original = app.font()
    try:
        enlarged = app.font()
        enlarged.setPointSizeF(20.0)
        app.setFont(enlarged)

        configure_application_font(app)

        assert app.font().pointSizeF() == pytest.approx(20.0)
        # ... while still having resolved the family to something concrete.
        assert app.font().family() not in ("", "Sans Serif", "sans-serif")
    finally:
        app.setFont(original)


def test_derived_type_sizes_follow_the_application_font(app) -> None:
    """The title and log sizes are multiples of the base size, not constants."""
    original = app.font()
    try:
        small = app.font()
        small.setPointSizeF(10.0)
        app.setFont(small)
        assert _base_point_size() == pytest.approx(10.0)
        small_sheet = build_stylesheet()

        large = app.font()
        large.setPointSizeF(20.0)
        app.setFont(large)
        assert _base_point_size() == pytest.approx(20.0)
        large_sheet = build_stylesheet()

        assert small_sheet != large_sheet
        # The title is the one deliberately oversized element; at 20pt base it must be
        # larger than the whole 10pt sheet's title, not pinned to a fixed pixel size.
        assert "20.0pt" in small_sheet      # title = 2x base
        assert "40.0pt" in large_sheet
    finally:
        app.setFont(original)


def test_stylesheet_sets_no_global_font_size(app) -> None:
    """No blanket font-size rule may reappear.

    A global ``font-size`` in the QSS overrides the application font for every widget,
    which is exactly how the OS text-size setting gets ignored a second time."""
    sheet = build_stylesheet()
    base_rule = sheet.split("QLabel {")[0]      # the QMainWindow/QWidget/QDialog block
    assert "font-size" not in base_rule
