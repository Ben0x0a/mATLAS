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
    assert data["match"] == {"type": "csv", "as_file": "Example Sheet.csv", "encoding": "utf-8-sig"}
    # The full attribute inventory is declared up front, before the mapping.
    assert data["expected_columns"] == ["Created Date/Time - UTC", "Origin Latitude", "Origin Longitude"]
    assertion = data["assertions"][0]
    assert assertion["position"]["latitude_wgs84"] == 'column("Origin Latitude")'
    assert assertion["position"]["longitude_wgs84"] == 'column("Origin Longitude")'
    assert assertion["time"]["instant"] == 'column("Created Date/Time - UTC")'
    assert assertion["links"] == {"entity_position": "at", "entity_time": "observed_at", "spatial_temporal": "instant"}


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
match: {type: csv, as_file: "Example Sheet.csv"}
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
