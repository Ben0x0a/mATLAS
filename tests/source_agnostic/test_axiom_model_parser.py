from __future__ import annotations

from pathlib import Path

import yaml

from utils.AXIOM_model_parser.axiom_model_parser import (
    default_output_dir,
    diff_with_presets,
    parse_axiom_model,
    write_templates,
)


def test_axiom_model_parser_extracts_attribute_table(tmp_path: Path) -> None:
    html = tmp_path / "Example Sheet.htm"
    html.write_text(
        """
        <html><head><title>Example Sheet</title></head><body>
        <table><tbody><tr><td>Description</td><td>Ignored</td></tr></tbody></table>
        <table>
          <thead><tr><th>Attribute</th><th>Description</th></tr></thead>
          <tbody>
            <tr><td>Created Date/Time - UTC</td><td>Created timestamp.</td></tr>
            <tr><td>Origin Latitude</td><td>Latitude.</td></tr>
            <tr><td>Origin Longitude</td><td>Longitude.</td></tr>
          </tbody>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    pages = parse_axiom_model(html)

    assert len(pages) == 1
    assert pages[0].title == "Example Sheet"
    assert [attribute.name for attribute in pages[0].attributes] == [
        "Created Date/Time - UTC",
        "Origin Latitude",
        "Origin Longitude",
    ]


def test_axiom_model_parser_writes_valid_template(tmp_path: Path) -> None:
    html = tmp_path / "Example Sheet.htm"
    html.write_text(
        """
        <html><head><title>Example Sheet</title></head><body>
        <table>
          <tr><th>Attribute</th><th>Description</th></tr>
          <tr><td>Created Date/Time - UTC</td><td>Created timestamp.</td></tr>
          <tr><td>Origin Latitude</td><td>Latitude.</td></tr>
          <tr><td>Origin Longitude</td><td>Longitude.</td></tr>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    written = write_templates(html, out_dir, "iOS")

    assert len(written) == 1
    data = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    text = written[0].read_text(encoding="utf-8")
    assert text.startswith("# AXIOM model reference")
    assert "# - Origin Latitude: Latitude." in text
    # v3 header
    assert data["preset"]["id"] == "ios.axiom.example_sheet"
    assert data["preset"]["name"] == "Example Sheet"
    assert data["preset"]["os"] == "iOS" and data["preset"]["tool"] == "AXIOM"
    # The full attribute inventory is declared up front, before the mapping.
    assert data["expected_columns"] == ["Created Date/Time - UTC", "Origin Latitude", "Origin Longitude"]
    # The generator GUESSES NO COLUMN: the selector filename and every column mapping are
    # TODO placeholders the analyst fills/prunes.
    assert data["input_selector"] == {"format": "csv", "name": "TODO_export_filename.csv", "encoding": "utf-8-sig"}
    assertion = data["assertions"][0]
    assert assertion["position"]["latitude_wgs84"] == 'column("TODO")'
    assert assertion["position"]["longitude_wgs84"] == 'column("TODO")'
    assert assertion["time"]["instant"] == 'column("TODO")'
    assert assertion["time"]["format"] == "TODO_STRPTIME"
    # Rich template: common + the enum links carry reviewed defaults (a TODO is not a valid
    # enum value); source_record_uid stays commented out (auto-generated).
    assert data["common"]["raw_source_path"] == 'column("TODO_Source")'
    assert assertion["links"] == {"entity_position": "at", "entity_time": "observed_at", "spatial_temporal": "instant"}
    assert "source_record_uid" not in data
    # The trip/interval alternative is offered as a commented block to swap in.
    assert "(B) a TRIP / dwell over an INTERVAL" in text
    assert "# optional position detail" in text


def test_generated_template_loads_but_lints_with_guidance(tmp_path: Path) -> None:
    """The skeleton is structurally loadable, but the linter surfaces exactly the
    decisions the analyst still owes — never a silent gap."""
    from model_atlas.presets.lint import lint_file

    html = tmp_path / "Trip.htm"
    html.write_text(
        "<html><head><title>Trip</title></head><body><table>"
        "<tr><th>Attribute</th><th>Description</th></tr>"
        "<tr><td>Latitude</td><td>x</td></tr><tr><td>Longitude</td><td>y</td></tr>"
        "<tr><td>Timestamp</td><td>t</td></tr></table></body></html>",
        encoding="utf-8",
    )
    written = write_templates(html, tmp_path / "out", "iOS")
    codes = {f.code for f in lint_file(written[0])}

    assert "parse-error" not in codes                      # it still loads
    assert "unfilled-placeholder" in codes                 # TODO filename/columns/format/entity
    assert "naive-timezone" in codes                       # format set, no zone -> analyst confirms
    assert "no-os-version" in codes                        # os_version left commented
    assert "mapped-not-declared" not in codes              # TODO placeholders are not phantom columns


def test_axiom_model_parser_diffs_expected_columns(tmp_path: Path) -> None:
    html = tmp_path / "Example Sheet.htm"
    html.write_text(
        """
        <html><head><title>Example Sheet</title></head><body>
        <table>
          <tr><th>Attribute</th><th>Description</th></tr>
          <tr><td>Timestamp</td><td>Timestamp.</td></tr>
          <tr><td>Latitude</td><td>Latitude.</td></tr>
          <tr><td>Longitude</td><td>Longitude.</td></tr>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    presets = tmp_path / "presets"
    presets.mkdir()
    existing = """
preset: {id: ios.axiom.example_sheet, name: Example Sheet, os: iOS, tool: AXIOM, version: 1.0, tier: secondary}
input_selector: {format: csv, name: "Example Sheet.csv"}
expected_columns: [Timestamp, Latitude]
assertions:
  - position: {latitude_wgs84: 'column("Latitude")'}
    time: {instant: 'column("Timestamp")', format: TODO}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""
    (presets / "example_sheet.yaml").write_text(existing, encoding="utf-8")

    diff = diff_with_presets(html, "iOS", presets)

    assert diff.missing == ()
    assert len(diff.expected_columns_modified) == 1
    preset_id, _, existing_columns, generated_columns = diff.expected_columns_modified[0]
    assert preset_id == "ios.axiom.example_sheet"
    assert existing_columns == ("Timestamp", "Latitude")               # declared inventory
    assert generated_columns == ("Timestamp", "Latitude", "Longitude")  # generator adds Longitude


def test_default_output_dir_lives_under_utils_results(tmp_path: Path) -> None:
    path = tmp_path / "Android - Location and Travel.zip"
    out = default_output_dir(path, "Android")

    assert "utils/AXIOM_model_parser/results/android/android_location_and_travel" in out.as_posix()
