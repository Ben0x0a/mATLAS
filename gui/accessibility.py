"""Accessibility helpers for the PySide6 GUI."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFontMetrics, QKeySequence
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


def configure_status_label(controller: Any) -> None:
    controller.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    controller.status_label.installEventFilter(controller)
    set_status(controller, "Idle")


def handle_status_resize(controller: Any, watched: QObject, event: object) -> None:
    if watched is controller.status_label and isinstance(event, QEvent):
        if event.type() == QEvent.Type.Resize:
            refresh_status_label(controller)


def set_status(controller: Any, text: str) -> None:
    controller._status_text = text
    controller.status_label.setToolTip(text)
    controller.status_label.setAccessibleDescription(text)
    refresh_status_label(controller)


def refresh_status_label(controller: Any) -> None:
    metrics = QFontMetrics(controller.status_label.font())
    width = max(20, controller.status_label.contentsRect().width())
    controller.status_label.setText(
        metrics.elidedText(controller._status_text, Qt.TextElideMode.ElideRight, width)
    )


def configure_accessibility(controller: Any) -> None:
    controller.window.setAccessibleName("mATLAS Transformer")
    controller.window.setAccessibleDescription(
        "Main window for selecting a source, presets, output settings, and running a transform."
    )

    for widget_name, buddy in (
        ("sourceLabel", controller.source_edit),
        ("presetsPathLabel", controller.presets_path_edit),
        ("outputModeLabel", controller.merge_outputs_check),
        ("mergeOutputLabel", controller.merge_outputs_check),
        ("dumpFullUfdrLabel", controller.dump_full_ufdr_check),
        ("outputLabel", controller.output_edit),
        ("traceabilityLabel", controller.traceability_combo),
        ("traceabilityOptionLabel", controller.traceability_combo),
        ("logLevelLabel", controller.log_level_combo),
        ("presetModeLabel", controller.auto_preset_check),
    ):
        if (label := _label(controller, widget_name)) is not None:
            label.setBuddy(buddy)

    if (title := _label(controller, "appTitleLabel")) is not None:
        title.setAccessibleName("mATLAS Transformer title")
    for step, text in {
        2: "Transformation step 1, standby robot",
        3: "Transformation step 2, robot activating",
        4: "Transformation step 3, robot compacting",
        5: "Transformation step 4, mattress forming",
        6: "Transformation step 5, bed form",
        7: "Transformation step 6, complete mattress",
    }.items():
        if (image := _label(controller, f"headerImage{step}")) is not None:
            image.setAccessibleName(text)

    controller.source_edit.setAccessibleName("Source path")
    controller.source_edit.setAccessibleDescription("Input CSV, Excel, SQLite, ZIP file, or folder to process.")
    controller.source_file_button.setAccessibleName("Browse for source file")
    controller.source_file_button.setAccessibleDescription("Choose a source file for processing.")
    controller.source_folder_button.setAccessibleName("Browse for source folder")
    controller.source_folder_button.setAccessibleDescription("Choose a source folder for processing.")

    controller.presets_path_edit.setAccessibleName("Preset root path")
    controller.presets_path_edit.setAccessibleDescription("Preset folder or YAML preset file used for matching.")
    controller.presets_folder_button.setAccessibleName("Browse for preset folder")
    controller.presets_file_button.setAccessibleName("Browse for preset YAML file")

    controller.merge_outputs_check.setAccessibleName("Merge output")
    controller.merge_outputs_check.setAccessibleDescription(
        "When checked, write one merged CSV. When unchecked, write one CSV per preset."
    )
    controller.dump_full_ufdr_check.setAccessibleName("Dump full UFDR")
    controller.dump_full_ufdr_check.setAccessibleDescription("Future option. Currently disabled.")

    controller.traceability_combo.setAccessibleName("Traceability format")
    controller.traceability_combo.setAccessibleDescription("Choose readable traceability or PROV JSON traceability.")
    controller.log_level_combo.setAccessibleName("Log level")
    controller.log_level_combo.setAccessibleDescription(
        "Choose the verbosity of messages written to the log window."
    )

    controller.auto_preset_check.setAccessibleName("Auto apply presets")
    controller.auto_preset_check.setAccessibleDescription(
        "When checked, all presets matching their selectors are used automatically."
    )
    controller.preset_status_label.setAccessibleName("Preset load status")
    controller.preset_search_edit.setAccessibleName("Search preset names")
    controller.preset_search_edit.setAccessibleDescription("Filter the available preset list by name.")
    controller.available_preset_list.setAccessibleName("Available presets")
    controller.available_preset_list.setAccessibleDescription(
        "Presets loaded from the preset root and matching the search."
    )
    controller.selected_preset_list.setAccessibleName("Selected presets")
    controller.selected_preset_list.setAccessibleDescription(
        "Presets selected for selected-preset mode or profile saving."
    )
    controller.add_preset_button.setAccessibleName("Add selected preset")
    controller.add_preset_button.setAccessibleDescription(
        "Move selected available presets into the selected preset list."
    )
    controller.remove_preset_button.setAccessibleName("Remove selected preset")
    controller.remove_preset_button.setAccessibleDescription("Remove selected presets from the selected preset list.")
    controller.load_profile_button.setAccessibleName("Load profile")
    controller.load_profile_button.setAccessibleDescription("Load a mATLAS profile containing preset selections.")
    controller.save_profile_button.setAccessibleName("Save profile")
    controller.save_profile_button.setAccessibleDescription("Save the selected preset list as a mATLAS profile.")

    controller.clear_log_button.setAccessibleName("Clear log")
    controller.clear_form_button.setAccessibleName("Clear form")
    controller.open_output_folder_button.setAccessibleName("Open output folder")
    controller.run_button.setAccessibleName("Run transform")
    controller.status_label.setAccessibleName("Status")
    controller.log_edit.setAccessibleName("Run log")
    controller.log_edit.setAccessibleDescription("Read-only log window containing processing messages.")

    _configure_shortcuts(controller)
    _configure_tab_order(controller)
    update_output_accessibility(controller)


def update_output_accessibility(controller: Any) -> None:
    if controller.merge_outputs_check.isChecked():
        controller.output_edit.setAccessibleName("Output CSV path")
        controller.output_edit.setAccessibleDescription("Path for the merged CSV output file.")
        controller.output_button.setAccessibleName("Choose output CSV path")
        controller.output_button.setAccessibleDescription("Choose where to save the merged CSV output file.")
    else:
        controller.output_edit.setAccessibleName("Output folder path")
        controller.output_edit.setAccessibleDescription("Folder that receives one CSV per matched preset.")
        controller.output_button.setAccessibleName("Choose output folder")
        controller.output_button.setAccessibleDescription("Choose the folder for per-preset CSV outputs.")


def _label(controller: Any, name: str) -> QLabel | None:
    return controller.window.findChild(QLabel, name)


def _configure_shortcuts(controller: Any) -> None:
    for button, shortcut in (
        (controller.run_button, "Ctrl+Return"),
        (controller.open_output_folder_button, "Ctrl+O"),
        (controller.load_profile_button, "Ctrl+P"),
        (controller.save_profile_button, "Ctrl+S"),
        (controller.clear_log_button, "Ctrl+L"),
        (controller.clear_form_button, "Ctrl+Shift+L"),
    ):
        button.setShortcut(QKeySequence(shortcut))


def _configure_tab_order(controller: Any) -> None:
    widgets: tuple[QWidget, ...] = (
        controller.source_edit,
        controller.source_file_button,
        controller.source_folder_button,
        controller.presets_path_edit,
        controller.presets_folder_button,
        controller.presets_file_button,
        controller.merge_outputs_check,
        controller.output_edit,
        controller.output_button,
        controller.traceability_combo,
        controller.log_level_combo,
        controller.auto_preset_check,
        controller.load_profile_button,
        controller.save_profile_button,
        controller.preset_search_edit,
        controller.available_preset_list,
        controller.add_preset_button,
        controller.remove_preset_button,
        controller.selected_preset_list,
        controller.clear_log_button,
        controller.clear_form_button,
        controller.open_output_folder_button,
        controller.run_button,
        controller.log_edit,
    )
    for first, second in zip(widgets, widgets[1:]):
        QWidget.setTabOrder(first, second)
