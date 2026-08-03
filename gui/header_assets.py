"""Header/window image assets and SVG-to-pixmap rendering for the PySide6 GUI.

Defines:    DESKTOP_ICON_ASSET / TRANSITION_ASSETS (asset filenames), asset_path(name)
            resolving a packaged asset, and configure_header_assets(window) which paints
            the six transition SVGs into the headerImage2..7 labels at device pixel
            ratio. _render_svg_pixmap / _device_pixel_ratio back the rendering.
Used by:    gui.app (MainController sets the window icon and configures the header row).
Depends on: PySide6 (QtGui/QtSvg/QtWidgets), importlib.resources for packaged assets.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QSizePolicy

DESKTOP_ICON_ASSET = "matlas_transformer_desktop_icon.svg"
TRANSITION_ASSETS = {
    2: "matlas_transition_01_standby.svg",
    3: "matlas_transition_02_activate.svg",
    4: "matlas_transition_03_compact.svg",
    5: "matlas_transition_04_reform.svg",
    6: "matlas_transition_05_bed_form.svg",
    7: "matlas_transition_06_complete.svg",
}


def asset_path(name: str) -> Path:
    return Path(files("gui").joinpath("assets", name))


# A square slot, so the six frames read as one filmstrip regardless of their differing
# intrinsic aspect ratios (the earlier 80x60 letterbox scaled each frame by a different
# factor, which is what made them look like unrelated, randomly sized mascots).
HEADER_FRAME_SIZE = 44


def configure_header_assets(window: QMainWindow) -> None:
    for number in range(2, 8):
        label = window.findChild(QLabel, f"headerImage{number}")
        if label is None:
            continue
        label.setFixedSize(HEADER_FRAME_SIZE, HEADER_FRAME_SIZE)
        policy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # Keep the frame's slot in the layout even while it is hidden. The frames are the
        # tallest thing in the header row, so without this, hiding them for UFDR dump mode
        # collapsed the row onto the shorter title and shunted the entire page up by 6px —
        # a visible jolt every time the mode was toggled.
        policy.setRetainSizeWhenHidden(True)
        label.setSizePolicy(policy)
        pixmap = _render_svg_pixmap(asset_path(TRANSITION_ASSETS[number]), label)
        if not pixmap.isNull():
            label.setPixmap(pixmap)


def set_transition_visible(window: QMainWindow, visible: bool) -> None:
    """Show or hide the six header frames.

    They illustrate the transform pipeline, so they are actively misleading in UFDR dump
    mode, which does not run it. The title beside them already swaps; hiding the frames
    keeps the picture and the words telling the same story."""
    for number in range(2, 8):
        label = window.findChild(QLabel, f"headerImage{number}")
        if label is not None:
            label.setVisible(visible)


def _render_svg_pixmap(path: Path, label: QLabel) -> QPixmap:
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return QPixmap()

    target = label.size()
    default_size = renderer.defaultSize()
    if default_size.isValid() and default_size.width() > 0 and default_size.height() > 0:
        aspect = default_size.width() / default_size.height()
        if target.width() / target.height() > aspect:
            render_height = target.height()
            render_width = int(render_height * aspect)
        else:
            render_width = target.width()
            render_height = int(render_width / aspect)
    else:
        render_width = target.width()
        render_height = target.height()

    dpr = _device_pixel_ratio()
    pixmap = QPixmap(max(1, int(target.width() * dpr)), max(1, int(target.height() * dpr)))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)

    x = (target.width() - render_width) / 2
    y = (target.height() - render_height) / 2
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(x, y, render_width, render_height))
    painter.end()
    return pixmap


def _device_pixel_ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    return float(screen.devicePixelRatio()) if screen is not None else 1.0
