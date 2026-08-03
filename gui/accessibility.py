"""Accessibility helpers for the PySide6 GUI.

Defines:    configure_accessibility (accessible names/descriptions, label buddies,
            shortcuts, and keyboard tab order for the main window, its tab shell, and
            the Transform form), configure_status_label / set_status /
            handle_status_resize / refresh_status_label (elided status label with a
            stable accessible description), and update_case_bar_accessibility.
Used by:    gui.app (MainController calls these during window construction).
Depends on: PySide6 widgets only; reads controller attributes bound in gui.app.
"""
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

    # Tab shell: name the tab widget, its tab bar, and each page so a screen reader
    # announces which section is active and what the other tabs offer. QTabWidget itself
    # provides the keyboard behaviour (arrow keys, Ctrl+Tab); only the naming is ours.
    controller.tab_widget.setAccessibleName("Main sections")
    controller.tab_widget.setAccessibleDescription(
        "Tabs switching between the Transform form, Database Edit, and Spatio-temporal concomitance sections."
    )
    controller.tab_widget.tabBar().setAccessibleName("Main section tabs")
    controller.transform_tab.setAccessibleName("Transform tab")
    controller.transform_tab.setAccessibleDescription(
        "Form for selecting a source, presets, and output settings, and running a transform."
    )
    # Both descriptions once said "placeholder ... arrives in a later update"; the panels
    # have since been built, so a screen-reader user was being told a working tool was not
    # there yet.
    controller.database_edit_tab.setAccessibleName("Database Edit tab")
    controller.database_edit_tab.setAccessibleDescription(
        "Browse and edit the tables of an open case database."
    )
    controller.spatiotemporal_tab.setAccessibleName("Spatio-temporal concomitance tab")
    controller.spatiotemporal_tab.setAccessibleDescription(
        "Find pairs of entities recorded close together in both space and time, within "
        "a chosen distance and a chosen time gap."
    )

    # The three check boxes carry their own text (and their own &mnemonic), so they need no
    # buddy label: one accessible node per control, and the whole caption is a click target.
    for widget_name, buddy in (
        ("sourceLabel", controller.source_edit),
        ("identityLabel", controller.entity_edit),
        ("entityLabel", controller.entity_edit),
        ("concernsLabel", controller.concerns_edit),
        ("presetsPathLabel", controller.presets_path_edit),
        ("optionsLabel", controller.traceability_combo),
        ("traceabilityOptionLabel", controller.traceability_combo),
        ("logLevelLabel", controller.log_level_combo),
        ("availablePresetHeaderLabel", controller.available_preset_list),
        ("selectedPresetHeaderLabel", controller.selected_preset_list),
    ):
        if (label := _label(controller, widget_name)) is not None:
            label.setBuddy(buddy)

    if (title := _label(controller, "appTitleLabel")) is not None:
        title.setAccessibleName("mATLAS Transformer title")
    # The six header frames are decorative: they repeat, in pictures, what the title next to
    # them already says. Naming each one made a screen reader announce six mascot
    # descriptions before the user reached a single control, so they are silenced instead —
    # no information is lost, because none of them carries any.
    for step in range(2, 8):
        if (image := _label(controller, f"headerImage{step}")) is not None:
            image.setAccessibleName("")
            image.setAccessibleDescription("")

    controller.source_edit.setAccessibleName("Source path")
    controller.source_edit.setAccessibleDescription(
        "Input CSV, Excel, XML, SQLite, ZIP, or UFDR file, or a folder, to process."
    )
    controller.source_file_button.setAccessibleName("Browse for source file")
    controller.source_file_button.setAccessibleDescription("Choose a source file for processing.")
    controller.source_folder_button.setAccessibleName("Browse for source folder")
    controller.source_folder_button.setAccessibleDescription("Choose a source folder for processing.")
    controller.entity_edit.setAccessibleName("Entity")
    controller.entity_edit.setAccessibleDescription("Optional source entity or owner added to output rows.")
    controller.concerns_edit.setAccessibleName("Concerns")
    controller.concerns_edit.setAccessibleDescription(
        "Required person, company, account, or case subject linked to output rows. "
        "Accepts a literal, or a preset-style reference such as field(Evidence number) "
        "to take the value from that source field per row."
    )

    controller.new_case_button.setAccessibleName("New case")
    controller.new_case_button.setAccessibleDescription(
        "Create a case: asks for a case name, then the folder to write it in. The case "
        "database is written there as <name>.matlas.sqlite.")
    controller.open_case_button.setAccessibleName("Open case")
    controller.open_case_button.setAccessibleDescription(
        "Open an existing case database; a subsequent run appends to it.")
    controller.close_case_button.setAccessibleName("Close case")
    controller.close_case_button.setAccessibleDescription(
        "Close the open case. The case database on disk is left untouched.")
    controller.case_summary_label.setAccessibleName("Case summary")
    controller.case_summary_label.setAccessibleDescription("Running totals of the open case.")

    controller.presets_path_edit.setAccessibleName("Preset root path")
    controller.presets_path_edit.setAccessibleDescription("Preset folder or YAML preset file used for matching.")
    controller.presets_folder_button.setAccessibleName("Browse for preset folder")
    controller.presets_file_button.setAccessibleName("Browse for preset YAML file")

    controller.dump_full_ufdr_check.setAccessibleName("Dump full UFDR")
    controller.dump_full_ufdr_check.setAccessibleDescription(
        "When checked, switch to the standalone UFDR parser, which dumps every Cellebrite "
        "model to CSV. The centre title changes to UFDR Parser."
    )

    controller.include_source_columns_check.setAccessibleName("Include source columns")
    controller.include_source_columns_check.setAccessibleDescription(
        "When checked, the untouched source columns are carried through to the output "
        "alongside the mapped fields."
    )

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
    controller.single_preset_notice_label.setAccessibleName("Single preset selector notice")
    controller.single_preset_notice_label.setAccessibleDescription(
        "One preset selected: selector filename filters will be skipped."
    )
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
    controller.reload_presets_button.setAccessibleName("Reload presets")
    controller.reload_presets_button.setAccessibleDescription("Reload presets from the current preset root.")

    controller.clear_log_button.setAccessibleName("Clear log")
    controller.clear_form_button.setAccessibleName("Clear form")
    controller.open_output_folder_button.setAccessibleName("Open output folder")
    controller.run_button.setAccessibleName("Run transform")
    controller.status_label.setAccessibleName("Status")
    controller.log_edit.setAccessibleName("Run log")
    controller.log_edit.setAccessibleDescription("Read-only log window containing processing messages.")

    _configure_shortcuts(controller)
    _configure_tab_order(controller)
    update_case_bar_accessibility(controller)


def update_case_bar_accessibility(controller: Any) -> None:
    """Describe the case bar's current state on the summary label.

    The bar swaps which buttons exist as a case opens and closes. A screen reader announces
    that change only if something carries the new state, and the visible summary line is the
    natural place: it is already the bar's status text."""
    if getattr(controller, "_case_name", None) is None:
        controller.case_summary_label.setAccessibleDescription(
            "No case is open. Use New case or Open case to start one.")
        return
    controller.case_summary_label.setAccessibleDescription(
        f"Case {controller._case_name} is open. Its database and every export "
        f"(one file per format) are written to {controller._case_output_dir}.")


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
        # The tab widget comes first so keyboard users reach the section tabs before
        # entering the Transform form; the case bar (New/Open case) leads the form, then
        # focus flows into the input fields.
        controller.tab_widget,
        controller.new_case_button,
        controller.open_case_button,
        controller.close_case_button,
        controller.source_edit,
        controller.source_file_button,
        controller.source_folder_button,
        controller.entity_edit,
        controller.concerns_edit,
        controller.presets_path_edit,
        # YAML before Folder, matching the on-screen order of the pair.
        controller.presets_file_button,
        controller.presets_folder_button,
        controller.additional_export_button,
        controller.dump_full_ufdr_check,
        controller.include_source_columns_check,
        controller.traceability_combo,
        controller.log_level_combo,
        # The search field now leads the preset row on screen, so it leads here too:
        # tab order that disagrees with reading order is disorienting for anyone using the
        # keyboard, and doubly so for a screen-reader user with no layout to fall back on.
        controller.preset_search_edit,
        controller.auto_preset_check,
        controller.reload_presets_button,
        controller.load_profile_button,
        controller.save_profile_button,
        controller.available_preset_list,
        controller.add_preset_button,
        controller.remove_preset_button,
        controller.add_all_presets_button,
        controller.remove_all_presets_button,
        controller.selected_preset_list,
        controller.clear_log_button,
        controller.clear_form_button,
        controller.open_error_report_button,
        controller.open_output_folder_button,
        controller.run_button,
        controller.log_edit,
    )
    for first, second in zip(widgets, widgets[1:]):
        QWidget.setTabOrder(first, second)
