"""Qt stylesheet for the Model Atlas GUI.

Defines:    configure_application_font(app) (concrete UI font before any .ui load) and
            build_stylesheet() -> str, the single light QSS theme the main window applies.
Used by:    gui.app (MainController._configure_ui_defaults) and launcher.cli.
Depends on: PySide6 QtGui/QtWidgets, plus the packaged SVG assets under gui/assets.

Three constraints shape this sheet, and none of them are cosmetic:

* Cross-platform. The theme is fully explicit — every interactive widget states its own
  background, border and text colour — so the app looks identical on macOS, Windows and
  Linux and does not half-inherit the host's dark mode. Anything left unstyled falls back
  to native painting and reads as a foreign control, which is what the check boxes,
  scroll bars and menus were doing before.
* Font scale. No global ``font-size`` is set. The body text is whatever the platform's
  system UI font is (see ``configure_application_font``), so a user who has raised their
  OS text size gets a larger app. The two deliberate deviations (the header title and the
  monospaced log) are derived from that base size in ``_base_point_size`` rather than
  hardcoded, for the same reason. Sizes here are floors (``min-height``), never caps, so
  larger text grows its control instead of being clipped.
* Visible focus. QSS painting replaces the native focus ring, so every focusable widget
  restates one explicitly (WCAG 2.4.7). Deleting a ``:focus`` rule below silently makes
  the app unusable by keyboard.
"""
from __future__ import annotations

import sys
from importlib.resources import files

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

# Default when the application font reports neither a point nor a pixel size (headless /
# offscreen platforms do this). 10pt is the common desktop default.
_FALLBACK_POINT_SIZE = 10.0


def _system_font_family(kind: QFontDatabase.SystemFont, fallback: str) -> str:
    """Return a QSS-safe system font family."""
    family = QFontDatabase.systemFont(kind).family() or fallback
    if family.lower() in {"sans serif", "sans-serif", "serif", "monospace"}:
        family = fallback
    return family.replace('"', r'\"')


def _first_available_font(candidates: tuple[str, ...], fallback: str) -> str:
    available = {family.casefold(): family for family in QFontDatabase.families()}
    for candidate in candidates:
        family = available.get(candidate.casefold())
        if family:
            return family
    return fallback


def _default_ui_font() -> str:
    if sys.platform == "darwin":
        return _first_available_font(
            ("SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue", "Arial"),
            "Arial",
        )
    if sys.platform == "win32":
        return _first_available_font(("Segoe UI", "Arial"), "Segoe UI")
    return _first_available_font(
        ("Noto Sans", "DejaVu Sans", "Liberation Sans", "Arial"),
        "DejaVu Sans",
    )


def _default_mono_font() -> str:
    if sys.platform == "darwin":
        return _first_available_font(("SF Mono", "Menlo", "Monaco"), "Menlo")
    if sys.platform == "win32":
        return _first_available_font(
            ("Cascadia Mono", "Consolas", "Courier New"),
            "Consolas",
        )
    return _first_available_font(
        ("Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New"),
        "DejaVu Sans Mono",
    )


def configure_application_font(app: object) -> None:
    """Resolve the UI font family to a concrete one, before any .ui file is loaded.

    Only the FAMILY is replaced. The platform reports aliases like "Sans Serif" that Qt then
    spends ~80ms resolving on every lookup, so pinning a real family is worth doing — but the
    size must survive untouched, because it is where the user's OS text-size setting lives.

    ``app.setFont(QFont(family))`` is the trap: that constructor carries Qt's built-in 12pt
    default, so it silently overwrote whatever size the platform reported. Someone who had
    turned their system text up to 20pt for readability got a 12pt app. Mutating the existing
    font keeps the size, weight and style the platform chose and changes only the family."""
    font = app.font()
    font.setFamily(_system_font_family(
        QFontDatabase.SystemFont.GeneralFont,
        _default_ui_font(),
    ))
    app.setFont(font)


def _base_point_size() -> float:
    """Point size of the running application font.

    Everything sized in the sheet is expressed relative to this rather than pinned to a
    pixel value, so the app honours the OS text-size setting a low-vision user relies on.
    A font carrying only a pixel size is converted at the conventional 96 dpi.
    """
    app = QApplication.instance()
    font = app.font() if app is not None else QFont()
    point_size = font.pointSizeF()
    if point_size > 0:
        return point_size
    pixel_size = font.pixelSize()
    if pixel_size > 0:
        return pixel_size * 72.0 / 96.0
    return _FALLBACK_POINT_SIZE


def _asset_url(name: str) -> str:
    """Filesystem path to a packaged asset, in a form QSS ``url()`` accepts.

    Backslashes are escaped because QSS treats them as escape characters, which would
    otherwise corrupt every Windows path.
    """
    return str(files("gui").joinpath("assets", name)).replace("\\", "/")


def build_stylesheet() -> str:
    """Return a light, explicit QSS theme independent of OS dark mode."""
    base_pt = _base_point_size()
    substitutions = {
        "__COMBO_ARROW__": _asset_url("combo-chevron-down.svg"),
        "__CHECK_ON__": _asset_url("check-white.svg"),
        "__CHECK_DISABLED__": _asset_url("check-slate.svg"),
        "__SPIN_UP__": _asset_url("spin-up.svg"),
        "__SPIN_DOWN__": _asset_url("spin-down.svg"),
        "__UI_FONT__": _system_font_family(
            QFontDatabase.SystemFont.GeneralFont,
            _default_ui_font(),
        ),
        "__MONO_FONT__": _system_font_family(
            QFontDatabase.SystemFont.FixedFont,
            _default_mono_font(),
        ),
        # The title is the one intentionally oversized element; the log is nudged down a
        # little because monospaced faces run visually larger at the same point size.
        "__TITLE_PT__": f"{base_pt * 2.0:.1f}",
        "__SECTION_PT__": f"{base_pt * 0.92:.1f}",
        "__LOG_PT__": f"{base_pt * 0.94:.1f}",
    }
    sheet = _QSS
    for placeholder, value in substitutions.items():
        sheet = sheet.replace(placeholder, value)
    return sheet


# Palette, for reference when editing below:
#   page  #eef2f7   pane  #f7f9fc   card #ffffff   field #ffffff
#   line  #c2cede (field) / #dbe3ee (card)
#   text  #1f2937 (body) / #46566b (secondary) / #16233a (emphasis)
#   accent #2563eb, accent-pressed #1d4ed8, accent-wash #dbeafe
# Disabled pairs are chosen to clear 4.5:1 against their own background; the previous
# #94a3b8-on-#f1f5f9 combination sat near 2:1 and made disabled controls unreadable
# rather than merely quiet.
_QSS = """
    QMainWindow, QWidget#centralwidget, QDialog {
        background: #eef2f7;
        color: #1f2937;
        font-family: "__UI_FONT__";
    }

    QLabel {
        color: #46566b;
        background: transparent;
    }
    QLabel:disabled {
        color: #7d8a9b;
    }
    QLabel#statusLabel, QLabel#presetStatusLabel {
        color: #33415c;
        font-weight: 600;
    }
    QLabel#singlePresetNoticeLabel {
        color: #8a5a00;
    }
    QLabel#availablePresetHeaderLabel, QLabel#selectedPresetHeaderLabel {
        color: #46566b;
        font-size: __SECTION_PT__pt;
        font-weight: 700;
    }
    QLabel#appTitleLabel {
        color: #16233a;
        font-size: __TITLE_PT__pt;
        font-weight: 800;
        padding: 0 12px;
    }

    /* Surface hierarchy: page (window) -> pane (tab body) -> card (group box) -> field.
       Each step is one shade lighter, so the cards read as raised without every level
       repeating the same border. */
    /* Tight top and bottom, generous left and right: the page is short on vertical room
       and has plenty of horizontal room, so the two axes are not padded alike. */
    QGroupBox {
        color: #243447;
        font-weight: 700;
        border: 1px solid #dbe3ee;
        border-radius: 8px;
        margin-top: 10px;
        padding: 10px 14px 10px 14px;
        background: #ffffff;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 14px;
        padding: 0 6px;
        background: #ffffff;
        color: #243447;
    }

    QLineEdit, QComboBox, QListWidget, QTextEdit, QAbstractSpinBox {
        background: #ffffff;
        color: #111827;
        border: 1px solid #c2cede;
        border-radius: 6px;
        padding: 3px 9px;
        selection-background-color: #2563eb;
        selection-color: #ffffff;
    }
    /* min-height belongs only on the single-line controls. Qt's stylesheet style pushes a
       QSS min-height down onto the widget's minimumSize, overwriting whatever the .ui set
       — so applying it to the lists and the log would flatten them to one row's height and
       silently discard their designed minimums.

       This value, the padding above and the border together must come to the same box
       height as a button (min-height 20 + 3+3 padding + 1+1 border = 28px), so a field and
       the browse button beside it line up on both edges. Change one, change the other. */
    QLineEdit, QComboBox, QAbstractSpinBox {
        min-height: 20px;
    }
    QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {
        border-color: #9db0c5;
    }
    /* Focus is shown as a doubled accent border rather than a colour change alone, so it
       survives greyscale and colour-vision differences. */
    QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTextEdit:focus,
    QAbstractSpinBox:focus {
        border: 2px solid #2563eb;
        padding: 2px 8px;
    }
    QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled, QTextEdit:disabled,
    QAbstractSpinBox:disabled {
        color: #6b7a8d;
        background: #f2f5f9;
        border-color: #d5dee9;
    }

    QComboBox::drop-down {
        width: 26px;
        border: none;
        border-left: 1px solid #d5dee9;
        background: #f5f8fc;
        border-top-right-radius: 6px;
        border-bottom-right-radius: 6px;
    }
    QComboBox::down-arrow {
        image: url(__COMBO_ARROW__);
        width: 12px;
        height: 8px;
        margin-right: 7px;
    }
    QComboBox QAbstractItemView {
        background: #ffffff;
        color: #111827;
        border: 1px solid #c2cede;
        border-radius: 6px;
        padding: 4px;
        selection-background-color: #dbeafe;
        selection-color: #16233a;
        outline: none;
    }

    QListWidget {
        padding: 3px;
        outline: none;
    }
    /* Rows are compact by design: this list is scanned, not read, so fitting more preset
       names in view matters more than airiness. The padding still leaves each row well
       above the ~24px minimum for a comfortable pointer target. */
    QListWidget::item {
        color: #111827;
        padding: 3px 7px;
        border: 1px solid transparent;
        border-radius: 5px;
    }
    QListWidget::item:hover {
        background: #f3f7fd;
    }
    /* Selection must not depend on focus: these two lists exchange items, so the user
       needs to see what is selected in the list they just left. */
    QListWidget::item:selected, QListWidget::item:selected:active,
    QListWidget::item:selected:!active {
        background: #dbeafe;
        color: #16233a;
        border-color: #9cc0f5;
    }

    /* Buttons are padded horizontally but kept tight vertically. The generous vertical
       padding was the single biggest consumer of height on this page — six rows of browse
       buttons, the action row and the transfer column all paid for it — while adding
       nothing to legibility or to how easy the buttons are to hit, which is governed by
       their full box. The horizontal padding is left wide so labels keep their air. */
    QPushButton, QToolButton {
        background: #ffffff;
        color: #1f2937;
        border: 1px solid #c2cede;
        border-radius: 6px;
        padding: 3px 14px;
        min-height: 20px;
        font-weight: 600;
    }
    QToolButton {
        /* Room on the right for the menu indicator, which is drawn inside the padding. */
        padding-right: 26px;
    }
    QPushButton:hover, QToolButton:hover {
        background: #eef4fb;
        border-color: #9db0c5;
    }
    QPushButton:pressed, QToolButton:pressed {
        background: #dbeafe;
        border-color: #2563eb;
    }
    QPushButton:focus, QToolButton:focus {
        border: 2px solid #2563eb;
        padding: 2px 13px;
    }
    QToolButton:focus {
        padding-right: 25px;
    }
    /* Spin boxes. The up/down buttons fell through to native painting — two tiny stacked
       arrows in a foreign style beside an otherwise flat field. Styled here to match the
       combo's drop-down: one bordered column on the field's right edge. */
    QAbstractSpinBox {
        padding-right: 20px;   /* room for the button column */
    }
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
        subcontrol-origin: border;
        width: 18px;
        background: #f5f8fc;
        border-left: 1px solid #d5dee9;
    }
    QAbstractSpinBox::up-button {
        subcontrol-position: top right;
        border-top-right-radius: 6px;
        border-bottom: 1px solid #d5dee9;
    }
    QAbstractSpinBox::down-button {
        subcontrol-position: bottom right;
        border-bottom-right-radius: 6px;
    }
    QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {
        background: #dbeafe;
    }
    QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {
        background: #bfd8fa;
    }
    QAbstractSpinBox::up-arrow {
        image: url(__SPIN_UP__);
        width: 9px;
        height: 6px;
    }
    QAbstractSpinBox::down-arrow {
        image: url(__SPIN_DOWN__);
        width: 9px;
        height: 6px;
    }
    QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled {
        opacity: 0.4;
    }

    /* The run-progress bar: a slim status indicator, not a page element. */
    QProgressBar {
        background: #ffffff;
        border: 1px solid #c2cede;
        border-radius: 5px;
        max-height: 10px;
        text-align: center;
    }
    QProgressBar::chunk {
        background: #2563eb;
        border-radius: 4px;
    }

    QPushButton:disabled, QToolButton:disabled {
        background: #f2f5f9;
        color: #6b7a8d;
        border-color: #d5dee9;
    }
    QToolButton::menu-indicator {
        subcontrol-position: right center;
        subcontrol-origin: padding;
        right: 8px;
    }

    /* The four transfer buttons are utility controls stacked beside the preset lists, and
       their column sets the height of the whole picker. Tighter still than the rest, so
       the lists next to them are not forced taller than their own content needs. */
    QPushButton#addPresetButton, QPushButton#removePresetButton,
    QPushButton#addAllPresetsButton, QPushButton#removeAllPresetsButton {
        padding: 1px 10px;
        min-height: 16px;
        font-weight: 500;
    }
    QPushButton#addPresetButton:focus, QPushButton#removePresetButton:focus,
    QPushButton#addAllPresetsButton:focus, QPushButton#removeAllPresetsButton:focus {
        padding: 0 9px;
    }

    QPushButton#runButton {
        background: #2563eb;
        color: #ffffff;
        border-color: #1d4ed8;
        padding: 3px 24px;
    }
    QPushButton#runButton:hover {
        background: #1d4ed8;
        border-color: #1d4ed8;
    }
    QPushButton#runButton:pressed {
        background: #1a44bd;
        border-color: #1a44bd;
    }
    QPushButton#runButton:focus {
        border: 2px solid #16233a;
        padding: 2px 23px;
    }
    QPushButton#runButton:disabled {
        background: #dbe7fb;
        color: #55647a;
        border-color: #c3d6f0;
    }

    /* Check boxes. Unstyled, these fell through to native painting and appeared as a
       black square (disabled, macOS) beside a system tick — two different controls on one
       row. The indicator is deliberately larger than the text so the hit target stays
       comfortable, and the caption now lives on the check box itself, making the whole
       label clickable. */
    QCheckBox, QRadioButton {
        color: #33415c;
        background: transparent;
        spacing: 8px;
        padding: 3px 2px;
    }
    QCheckBox:disabled, QRadioButton:disabled {
        color: #7d8a9b;
    }
    QCheckBox::indicator, QRadioButton::indicator {
        width: 17px;
        height: 17px;
        border: 1px solid #97a8bd;
        border-radius: 4px;
        background: #ffffff;
    }
    QRadioButton::indicator {
        border-radius: 9px;
    }
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {
        border-color: #2563eb;
    }
    QCheckBox::indicator:checked {
        background: #2563eb;
        border-color: #1d4ed8;
        image: url(__CHECK_ON__);
    }
    QRadioButton::indicator:checked {
        background: #2563eb;
        border-color: #1d4ed8;
    }
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
        background: #f2f5f9;
        border-color: #cbd5e1;
    }
    QCheckBox::indicator:checked:disabled {
        background: #e6ebf2;
        border-color: #cbd5e1;
        image: url(__CHECK_DISABLED__);
    }
    /* The check box paints its own focus ring: QSS suppresses the native one, and without
       this a keyboard user cannot tell which box they are about to toggle. */
    QCheckBox:focus, QRadioButton:focus {
        border: 1px solid #2563eb;
        border-radius: 5px;
        padding: 2px 1px;
    }

    /* Tab shell. The pane is a plain surface, not a second card: it previously repeated
       the group box's border, radius and background, so every panel sat in a box inside
       an identical box. */
    QTabWidget::pane {
        border: 1px solid #dbe3ee;
        border-radius: 8px;
        background: #f7f9fc;
        top: -1px;
    }
    QTabWidget::tab-bar {
        left: 8px;
    }
    QTabBar::tab {
        background: #e3eaf3;
        color: #46566b;
        border: 1px solid #dbe3ee;
        border-bottom: none;
        border-top-left-radius: 7px;
        border-top-right-radius: 7px;
        padding: 8px 18px;
        margin-right: 2px;
        font-weight: 600;
    }
    QTabBar::tab:selected {
        background: #f7f9fc;
        color: #16233a;
    }
    QTabBar::tab:hover:!selected {
        background: #eef4fb;
    }
    QTabBar::tab:focus {
        border: 2px solid #2563eb;
        border-bottom: none;
        padding: 7px 17px;
    }

    /* Scroll bars. Native ones were the last dark element on the page. Both orientations
       and the corner are styled, or Windows shows a mismatched square where they meet. */
    QScrollBar:vertical {
        background: transparent;
        width: 12px;
        margin: 2px;
    }
    QScrollBar:horizontal {
        background: transparent;
        height: 12px;
        margin: 2px;
    }
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
        background: #b8c6d8;
        border-radius: 4px;
    }
    QScrollBar::handle:vertical {
        min-height: 28px;
    }
    QScrollBar::handle:horizontal {
        min-width: 28px;
    }
    QScrollBar::handle:hover {
        background: #93a6bd;
    }
    QScrollBar::handle:pressed {
        background: #6f8399;
    }
    QScrollBar::add-line, QScrollBar::sub-line {
        height: 0;
        width: 0;
        border: none;
        background: transparent;
    }
    QScrollBar::add-page, QScrollBar::sub-page {
        background: transparent;
    }
    QAbstractScrollArea::corner {
        background: transparent;
        border: none;
    }

    /* Tables (the Database Edit grid). Unstyled, a QTableView falls through to native
       painting and rendered as a black slab against the light page. The header is styled
       with it: leaving it native put dark grey column titles above light rows. */
    QTableView {
        background: #ffffff;
        alternate-background-color: #f7f9fc;
        color: #111827;
        border: 1px solid #c2cede;
        border-radius: 6px;
        gridline-color: #e2e8f0;
        selection-background-color: #dbeafe;
        selection-color: #16233a;
        outline: none;
    }
    QTableView::item {
        padding: 3px 6px;
    }
    QTableView::item:selected {
        background: #dbeafe;
        color: #16233a;
    }
    QHeaderView {
        background: transparent;
        border: none;
    }
    QHeaderView::section {
        background: #eef2f7;
        color: #33415c;
        border: none;
        border-right: 1px solid #dbe3ee;
        border-bottom: 1px solid #dbe3ee;
        padding: 4px 8px;
        font-weight: 600;
    }
    QHeaderView::section:last {
        border-right: none;
    }
    QTableView QTableCornerButton::section {
        background: #eef2f7;
        border: none;
        border-right: 1px solid #dbe3ee;
        border-bottom: 1px solid #dbe3ee;
    }

    /* Popups: the Additional export menu and every tooltip, which were the remaining
       native surfaces. */
    QMenu {
        background: #ffffff;
        color: #1f2937;
        border: 1px solid #c2cede;
        border-radius: 8px;
        padding: 5px;
    }
    QMenu::item {
        padding: 6px 26px 6px 26px;
        border-radius: 5px;
    }
    QMenu::item:selected {
        background: #dbeafe;
        color: #16233a;
    }
    QMenu::item:disabled {
        color: #6b7a8d;
    }
    QMenu::indicator {
        width: 15px;
        height: 15px;
        left: 7px;
        border: 1px solid #97a8bd;
        border-radius: 4px;
        background: #ffffff;
    }
    QMenu::indicator:checked {
        background: #2563eb;
        border-color: #1d4ed8;
        image: url(__CHECK_ON__);
    }
    QMenu::separator {
        height: 1px;
        background: #e2e8f0;
        margin: 5px 8px;
    }

    QToolTip {
        background: #16233a;
        color: #f8fafc;
        border: 1px solid #16233a;
        border-radius: 5px;
        padding: 5px 8px;
    }

    QTextEdit#logEdit {
        font-family: "__MONO_FONT__";
        font-size: __LOG_PT__pt;
    }
"""
