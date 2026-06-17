"""Qt stylesheet for the Model Atlas GUI."""
from __future__ import annotations

import sys
from importlib.resources import files

from PySide6.QtGui import QFont, QFontDatabase


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
    """Use a concrete UI font before loading .ui files."""
    ui_font = _system_font_family(
        QFontDatabase.SystemFont.GeneralFont,
        _default_ui_font(),
    )
    app.setFont(QFont(ui_font))


def build_stylesheet() -> str:
    """Return a light, explicit QSS theme independent of OS dark mode."""
    arrow = files("gui").joinpath("assets", "combo-chevron-down.svg")
    ui_font = _system_font_family(
        QFontDatabase.SystemFont.GeneralFont,
        _default_ui_font(),
    )
    mono_font = _system_font_family(
        QFontDatabase.SystemFont.FixedFont,
        _default_mono_font(),
    )
    return """
    QMainWindow, QWidget#centralwidget {
        background: #f6f8fb;
        color: #1f2937;
        font-family: "__UI_FONT__";
        font-size: 13px;
    }

    QLabel {
        color: #334155;
        background: transparent;
    }
    QLabel#statusLabel, QLabel#presetStatusLabel {
        color: #334155;
        font-weight: 600;
    }
    QLabel#appTitleLabel {
        color: #16233a;
        font-size: 30px;
        font-weight: 800;
        letter-spacing: 0px;
        padding: 2px 10px 6px 10px;
    }

    QGroupBox {
        color: #243447;
        font-weight: 700;
        border: 1px solid #c7d3e1;
        border-radius: 6px;
        margin-top: 12px;
        padding: 12px 10px 10px 10px;
        background: #f9fbfd;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 14px;
        padding: 0 6px;
        background: #f9fbfd;
        color: #243447;
    }

    QLineEdit, QComboBox, QListWidget, QTextEdit {
        background: #ffffff;
        color: #111827;
        border: 1px solid #b9c7d8;
        border-radius: 5px;
        padding: 6px 8px;
        selection-background-color: #2563eb;
        selection-color: #ffffff;
    }
    QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTextEdit:focus {
        border-color: #2563eb;
    }
    QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled, QTextEdit:disabled {
        color: #94a3b8;
        background: #f1f5f9;
        border-color: #d5dee9;
    }

    QComboBox {
        min-height: 24px;
    }
    QComboBox::drop-down {
        width: 28px;
        border: none;
        border-left: 1px solid #d5dee9;
        background: #f8fafc;
        border-top-right-radius: 5px;
        border-bottom-right-radius: 5px;
    }
    QComboBox::down-arrow {
        image: url(__COMBO_ARROW__);
        width: 12px;
        height: 8px;
        margin-right: 8px;
    }
    QComboBox QAbstractItemView {
        background: #ffffff;
        color: #111827;
        border: 1px solid #b9c7d8;
        selection-background-color: #dbeafe;
        selection-color: #111827;
        outline: none;
    }

    QListWidget {
        min-height: 150px;
        outline: none;
    }
    QListWidget::item {
        color: #111827;
        padding: 5px 7px;
        margin: 1px 4px;
        border: 1px solid transparent;
        border-radius: 4px;
    }
    QListWidget::item:selected {
        background: #eef4fb;
        color: #16233a;
        border-color: #d5e2f1;
    }
    QListWidget::item:selected:active, QListWidget::item:selected:!active {
        background: #eef4fb;
        color: #16233a;
        border-color: #d5e2f1;
    }

    QPushButton {
        background: #ffffff;
        color: #1f2937;
        border: 1px solid #b9c7d8;
        border-radius: 5px;
        padding: 6px 12px;
        min-height: 24px;
        font-weight: 600;
    }
    QPushButton:hover {
        background: #eef4fb;
        border-color: #9db0c5;
    }
    QPushButton:pressed {
        background: #dbeafe;
        border-color: #2563eb;
    }
    QPushButton:disabled {
        background: #f1f5f9;
        color: #94a3b8;
        border-color: #d5dee9;
    }
    QPushButton#runButton {
        background: #2563eb;
        color: #ffffff;
        border-color: #2563eb;
    }
    QPushButton#runButton:hover {
        background: #1d4ed8;
        border-color: #1d4ed8;
    }
    QPushButton#runButton:disabled {
        background: #bfdbfe;
        color: #f8fafc;
        border-color: #bfdbfe;
    }

    QTextEdit#logEdit {
        font-family: "__MONO_FONT__";
        font-size: 12px;
        line-height: 1.25;
    }
    """.replace("__COMBO_ARROW__", str(arrow)).replace(
        "__UI_FONT__",
        ui_font,
    ).replace("__MONO_FONT__", mono_font)
