"""Parse AXIOM model HTML pages and generate starter preset YAML.

The AXIOM help pages expose artifact columns in an Attribute/Description table.
This script extracts those tables from folders or ZIP archives and writes one
source-agnostic preset template per `.htm` page.
"""
from __future__ import annotations

import argparse
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import yaml

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

# The generator NEVER guesses a mapping: a wrong guess the analyst doesn't notice is a
# silent error. It emits placeholders the analyst must replace, and declares the source
# columns (``expected_columns``) so the analyst has the inventory to map FROM. Every
# mapping, selector value, link, pipe, and timestamp format is the analyst's decision.
_TODO_REF = 'column("TODO")'
_TODO_FILENAME = "TODO_export_filename.csv"
_TODO_FORMAT = "TODO_STRPTIME"


@dataclass(frozen=True)
class AxiomAttribute:
    name: str
    description: str


@dataclass(frozen=True)
class AxiomPage:
    title: str
    html_name: str
    attributes: tuple[AxiomAttribute, ...]


@dataclass(frozen=True)
class TemplateRecord:
    path: Path
    data: dict


@dataclass(frozen=True)
class PresetDiff:
    missing: tuple[str, ...]
    expected_columns_modified: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]
    unchanged: tuple[str, ...]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            text = " ".join("".join(self._current_cell).split())
            self._current_row.append(text)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self.tables.append(self._current_table)
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug or "preset"


def _parse_page(html_name: str, text: str) -> AxiomPage:
    parser = _TableParser()
    parser.feed(text)
    attributes: list[AxiomAttribute] = []
    for table in parser.tables:
        if not table:
            continue
        header = [cell.casefold() for cell in table[0]]
        if len(header) >= 2 and header[0] == "attribute" and header[1] == "description":
            for row in table[1:]:
                if len(row) >= 2 and row[0]:
                    attributes.append(AxiomAttribute(name=row[0], description=row[1]))
            break
    title = parser.title or Path(html_name).stem
    return AxiomPage(title=title, html_name=html_name, attributes=tuple(attributes))


def _iter_html_inputs(path: Path) -> Iterable[tuple[str, str]]:
    path = Path(path)
    if path.is_dir():
        for html_path in sorted(path.rglob("*.htm")):
            yield html_path.name, html_path.read_text(encoding="utf-8", errors="replace")
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in sorted(zf.namelist()):
                if name.casefold().endswith(".htm"):
                    yield Path(name).name, zf.read(name).decode("utf-8", errors="replace")
        return
    if path.is_file() and path.suffix.casefold() == ".htm":
        yield path.name, path.read_text(encoding="utf-8", errors="replace")
        return
    raise ValueError(f"Unsupported AXIOM model input: {path}")


def parse_axiom_model(path: Path) -> list[AxiomPage]:
    return [_parse_page(name, text) for name, text in _iter_html_inputs(path)]


def _yaml_str(value: str) -> str:
    """A YAML-safe inline scalar (quoted only when necessary), for embedding in the text
    template's header lines."""
    return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).splitlines()[0].strip()


def render_preset_text(page: AxiomPage, platform: str) -> str:
    """A RICH starter template the analyst prunes down, never a guess.

    Workflow: the parser lays out every common mapping as a ``column("TODO")`` placeholder
    plus commented alternatives (extra position detail, an interval/trip assertion). The
    analyst deletes what this export does not have, fills each TODO, and picks the assertion
    shape. Column mappings are NEVER guessed (a wrong column is a silent None); only the
    enum links carry a reviewed default (a TODO there is not a valid value) with the full
    option list inline. ``expected_columns`` is the parsed inventory to map FROM."""
    columns = [attribute.name for attribute in page.attributes]
    cols_block = yaml.safe_dump(columns, default_flow_style=False, allow_unicode=True).rstrip()
    preset_id = f"{_slug(platform)}.axiom.{_slug(page.title)}"
    return f'''preset:
  id: {preset_id}
  name: {_yaml_str(page.title)}
  os: {_yaml_str(platform)}
  tool: AXIOM
  # os_version: ">=15"             # applicability range, so version variants tie-break
  version: "0.1"
  tier: secondary

input_selector:
  format: csv                      # AXIOM exports are CSV (UTF-8 BOM)
  name: {_TODO_FILENAME}   # set to the actual export file name
  encoding: utf-8-sig

# Full source-column inventory parsed from the AXIOM page. The mapping below maps a SUBSET
# — delete columns you do not actually have, add any the page missed.
expected_columns:
{cols_block}

# AXIOM embeds the export timezone in each Date/Time header (e.g. "... - UTC+00:00 (dd.MM.yyyy)").
# This pattern captures that offset so the time block can record/apply it. (A "Local Time"
# header carries no offset — that needs the device zone in matlas_config.toml, handled later.)
patterns:
  tz: '(?P<z>UTC[+-]\\d{{2}}:\\d{{2}}(?:\\[DST\\])?)'

# Row-level fields shared by every assertion. Delete those you do not map.
common:
  entity: const("TODO_device_or_account")        # who/what the rows are about
  # entity_type: const("TODO")
  raw_source_path: column("TODO_Source")         # AXIOM 'Source' column — where the trace came from
# source_record_uid: column("TODO_Item ID")      # a STABLE tool id; leave commented to auto-generate

# Choose the assertion shape(s) that fit this artifact; delete the rest.
assertions:
# ----- (A) a POINT observed at an instant — the common case -----
- position:
    latitude_wgs84: column("TODO")
    longitude_wgs84: column("TODO")
    # optional position detail — delete any your export lacks:
    # altitude_m: column("TODO")
    # horizontal_accuracy_m: column("TODO")
    # vertical_accuracy_m: column("TODO")
    # horizontal_speed_kmh: {{ from: column("TODO"), unit: "m/s" }}
    # heading_deg: column("TODO")
  time:
    # For AXIOM use the wildcard form so it matches any export offset:
    #   column("Timestamp Date/Time - * (dd.MM.yyyy)")
    instant: column("TODO")
    format: {_TODO_FORMAT}          # e.g. "%d.%m.%Y %H:%M:%S.%f" for a (dd.MM.yyyy) header; OR use  epoch: unix_s | cocoa | ...
    # AXIOM Date/Time headers carry the export offset — capture it from the SAME column as
    # `instant` so UTC+00:00 records utc_offset_hours = 0.0 (and any offset is applied):
    zone: {{ from: 'header("TODO_same_DateTime_column")', pipe: "regex(tz, group=z)" }}
  links:
    entity_position: at             # at | within_range_of | at_fixed_detector | references | claimed_at | inferred
    entity_time: observed_at        # observed_at | event_at | recorded_at | reported_for | intended_for
    spatial_temporal: instant       # instant | continuous_during_interval | once_during_interval | sporadic_during_interval | never_during_interval

# ----- (B) a TRIP / dwell over an INTERVAL — uncomment, fill, and delete (A) if this fits -----
# - position:
#     latitude_wgs84: column("TODO")
#     longitude_wgs84: column("TODO")
#   time:
#     interval:
#       lower: column("TODO_start_time")
#       upper: column("TODO_end_time")
#     format: {_TODO_FORMAT}
#     zone: {{ from: 'header("TODO_start_DateTime_column")', pipe: "regex(tz, group=z)" }}
#   links:
#     entity_position: at
#     entity_time: event_at
#     spatial_temporal: continuous_during_interval
'''


def preset_template(page: AxiomPage, platform: str) -> dict:
    """The active (uncommented) preset of the rendered template — a single source of truth
    so callers/tests see exactly what the analyst gets."""
    return yaml.safe_load(render_preset_text(page, platform))


def _comment_header(page: AxiomPage, platform: str) -> str:
    lines = [
        "# AXIOM model reference — STARTER TEMPLATE (the parser guesses nothing)",
        f"# html_file: {page.html_name}",
        f"# title: {page.title}",
        f"# platform: {platform}",
        "# TODO (analyst): replace every column(\"TODO\") and TODO_ placeholder; set the export",
        "#   filename; choose the assertion links; map raw_source_path; set the timestamp",
        "#   format/zone; map source_record_uid if a stable tool id exists. Run the linter.",
        "# attributes (the columns you have, to map FROM):",
    ]
    for attribute in page.attributes:
        description = " ".join(attribute.description.split())
        lines.append(f"# - {attribute.name}: {description}")
    return "\n".join(lines) + "\n"


def default_output_dir(input_path: Path, platform: str) -> Path:
    return DEFAULT_RESULTS_DIR / _slug(platform) / _slug(Path(input_path).stem)


def build_templates(input_path: Path, platform: str) -> list[tuple[str, str]]:
    templates: list[tuple[str, str]] = []
    for page in parse_axiom_model(input_path):
        if not page.attributes:
            continue
        # Comment header, a blank line, then the rich (already-spaced) body.
        text = _comment_header(page, platform) + "\n" + render_preset_text(page, platform)
        templates.append((f"{_slug(page.title)}.yaml", text))
    return templates


def write_templates(input_path: Path, output_dir: Path, platform: str) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in build_templates(input_path, platform):
        path = output_dir / filename
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def _load_yaml(path: Path) -> dict | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None


def _preset_id(data: dict) -> str | None:
    preset = data.get("preset")
    if not isinstance(preset, dict):
        return None
    pid = preset.get("id")
    return pid if isinstance(pid, str) else None


def _expected_columns(data: dict) -> tuple[str, ...]:
    """The declared source-column inventory of a v3 preset (the differential basis)."""
    raw = data.get("expected_columns") or []
    if not isinstance(raw, list):
        return ()
    return tuple(str(value) for value in raw)


def _records_from_files(paths: Iterable[Path]) -> dict[str, TemplateRecord]:
    records: dict[str, TemplateRecord] = {}
    for path in paths:
        data = _load_yaml(path)
        if not data:
            continue
        name = _preset_id(data)
        if not name:
            continue
        records[name] = TemplateRecord(path=path, data=data)
    return records


def generated_records(input_path: Path, platform: str) -> dict[str, TemplateRecord]:
    records: dict[str, TemplateRecord] = {}
    for filename, text in build_templates(input_path, platform):
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            continue
        name = _preset_id(data)
        if name:
            records[name] = TemplateRecord(path=Path(filename), data=data)
    return records


def diff_with_presets(input_path: Path, platform: str, presets_dir: Path) -> PresetDiff:
    generated = generated_records(input_path, platform)
    existing = _records_from_files(sorted(Path(presets_dir).rglob("*.yaml")))
    missing: list[str] = []
    modified: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    unchanged: list[str] = []

    for parser_name, generated_record in sorted(generated.items()):
        existing_record = existing.get(parser_name)
        if existing_record is None:
            missing.append(parser_name)
            continue
        generated_columns = _expected_columns(generated_record.data)
        existing_columns = _expected_columns(existing_record.data)
        if generated_columns != existing_columns:
            modified.append(
                (
                    parser_name,
                    str(existing_record.path),
                    existing_columns,
                    generated_columns,
                )
            )
        else:
            unchanged.append(parser_name)
    return PresetDiff(
        missing=tuple(missing),
        expected_columns_modified=tuple(modified),
        unchanged=tuple(unchanged),
    )


def print_diff(diff: PresetDiff) -> None:
    print(f"Missing presets: {len(diff.missing)}")
    for parser_name in diff.missing:
        print(f"  MISSING {parser_name}")
    print(f"Expected-column changes: {len(diff.expected_columns_modified)}")
    for parser_name, path, existing, generated in diff.expected_columns_modified:
        removed = [column for column in existing if column not in generated]
        added = [column for column in generated if column not in existing]
        print(f"  MODIFIED {parser_name} ({path})")
        if removed:
            print(f"    removed: {removed}")
        if added:
            print(f"    added: {added}")
    print(f"Unchanged presets: {len(diff.unchanged)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="AXIOM model folder, ZIP, or .htm file.")
    parser.add_argument("--platform", required=True, help="Platform label, e.g. Android or iOS.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory for generated YAML templates. Defaults to "
            "utils/AXIOM_model_parser/results/<platform>/<input-name>/."
        ),
    )
    parser.add_argument(
        "--presets-dir",
        type=Path,
        default=Path("presets"),
        help="Existing presets directory to compare against.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Only print the comparison report; do not write generated templates.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir or default_output_dir(args.input, args.platform)
    diff = diff_with_presets(args.input, args.platform, args.presets_dir)
    print_diff(diff)
    if args.no_write:
        print("No templates written (--no-write).")
        return 0
    written = write_templates(args.input, output_dir, args.platform)
    print(f"Wrote {len(written)} template(s) to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
