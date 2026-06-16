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
    assert "axiom_model:" not in text
    assert data["selectors"] == [
        {"source_type": "csv", "file_name": "Example Sheet.csv"},
        {"source_type": "excel", "sheet_name": "Example Sheet"},
    ]
    location = data["location_mappings"][0]
    assert location["timestamp"] == "col:Created Date/Time - UTC"
    assert location["Latitude"] == "col:Origin Latitude"
    assert location["Longitude"] == "col:Origin Longitude"
    assert location["Temporal relation"] == "value:instant"
    assert location["raw_timestamp"] is None
    assert location["temporal_source"] is None
    assert location["raw_position"] is None
    assert location["position_source"] is None


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
    existing = yaml.safe_load(
        (
            "# comment\n"
            + """
name: AXIOM iOS - Example Sheet
parser:
  name: axiom_ios_example_sheet
  version: 1.0
selectors: []
expected_columns:
  - Timestamp
  - Latitude
model_mapping: []
"""
        )
    )
    (presets / "example_sheet.yaml").write_text(
        yaml.safe_dump(existing, sort_keys=False),
        encoding="utf-8",
    )

    diff = diff_with_presets(html, "iOS", presets)

    assert diff.missing == ()
    assert len(diff.expected_columns_modified) == 1
    parser_name, _, existing_columns, generated_columns = diff.expected_columns_modified[0]
    assert parser_name == "axiom_ios_example_sheet"
    assert existing_columns == ("Timestamp", "Latitude")
    assert generated_columns == ("Timestamp", "Latitude", "Longitude")


def test_default_output_dir_lives_under_utils_results(tmp_path: Path) -> None:
    path = tmp_path / "Android - Location and Travel.zip"
    out = default_output_dir(path, "Android")

    assert "utils/AXIOM_model_parser/results/android/android_location_and_travel" in out.as_posix()
