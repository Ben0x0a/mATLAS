"""Dump the report's header metadata (case, extraction, images...) to ``<base>_report_info.csv``.

Defines:    read_report_info (header -> rows), write_report_info (rows -> CSV) and the CSV's
            column names.
Used by:    dump (writes the CSV once per run); the workbook brick reads the CSV back for its
            Source device sheet.
Depends on: archive (open_report), const (traceability columns), lxml, csv.

WHAT the header is: everything ``<project>`` holds before ``<taggedFiles>`` (or
``<decodedData>``): the project attributes (report version, extraction type, model/node
counts), ``<sourceExtractions>``, ``<caseInformation>`` (examiner, case name/number...),
``<metadata>`` sections (extraction start/end, tool versions, time zone, report creation time)
and ``<images>`` (the acquisition files with their hashes). It is a few KB at the top of the
report, so it is read in a separate short pass and parsed whole — the decoded models never
touch it, which is why the model CSVs alone could not carry it.

Every value is written verbatim, one row per value; nothing is interpreted. Device identity
(IMEI, serial, ICCID...) is NOT in the header: Physical Analyzer decodes it into
``DeviceInfoEntry`` models, which are dumped like any other model.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from lxml import etree

from ufdr_parser.archive import open_report
from ufdr_parser.const import TRACE_COLUMNS

log = logging.getLogger(__name__)

REPORT_INFO_SUFFIX = "_report_info.csv"
COL_SECTION = "section"
COL_NAME = "name"
COL_VALUE = "value"
COL_DETAILS = "details"
REPORT_INFO_COLUMNS = (COL_SECTION, COL_NAME, COL_VALUE, COL_DETAILS) + TRACE_COLUMNS

# The header ends where the first of these bulky sections starts.
_HEADER_END_MARKERS = (b"<taggedFiles", b"<decodedData")
_READ_CHUNK = 64 * 1024
# The header is a few KB; a report without either marker within this many bytes is read no
# further (the header rows found so far are still written).
_HEADER_SEARCH_CAP = 16 * 1024 * 1024

Row = tuple[str, str, str | None, str | None]


def _local(tag: object) -> str:
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _text(elem: "etree._Element") -> str | None:
    text = "".join(elem.itertext()).strip()
    return text or None


def _read_header(path: Path) -> bytes:
    """The report bytes up to the first bulky section, as a closed, parseable document."""
    buffer = b""
    with open_report(path) as stream:
        while len(buffer) < _HEADER_SEARCH_CAP:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
            hits = [index for marker in _HEADER_END_MARKERS if (index := buffer.find(marker)) != -1]
            if hits:
                return buffer[: min(hits)] + b"</project>"
    log.warning(f"{path}: no <taggedFiles>/<decodedData> found in the first bytes; header may be partial")
    return buffer + b"</project>"


def _attribute_rows(section: str, elem: "etree._Element", skip: tuple[str, ...] = ()) -> list[Row]:
    return [(section, name, value, None) for name, value in elem.attrib.items() if name not in skip and "}" not in name]


def _generic_rows(section: str, elem: "etree._Element") -> list[Row]:
    """Any other header element: its attributes, then each child named by its ``name``/tag."""
    rows = _attribute_rows(section, elem)
    for child in elem:
        name = child.get("name") or _local(child.tag)
        if len(child):
            rows += _generic_rows(f"{section} / {name}", child)
        else:
            details = "; ".join(f"{k}={v}" for k, v in child.attrib.items() if k != "name") or None
            rows.append((section, name, _text(child), details))
    return rows


def read_report_info(path: Path) -> list[Row]:
    """(section, name, value, details) rows for every value of the report header."""
    root = etree.fromstring(_read_header(Path(path)), parser=etree.XMLParser(recover=True, huge_tree=True))
    if root is None:
        return []
    rows: list[Row] = _attribute_rows("Report", root)
    for child in root:
        tag = _local(child.tag)
        if tag == "sourceExtractions":
            for info in child:
                label = info.get("index") or info.get("id") or "?"
                rows += _attribute_rows(f"Source extraction {label}", info)
        elif tag == "caseInformation":
            for field in child:
                details = f"fieldType={field.get('fieldType')}" if field.get("fieldType") else None
                rows.append(("Case information", field.get("name") or _local(field.tag), _text(field), details))
        elif tag == "metadata":
            section = f"Metadata: {child.get('section') or '(no section)'}"
            for item in child:
                extraction = item.get("sourceExtraction")
                rows.append((section, item.get("name") or _local(item.tag), _text(item),
                             f"sourceExtraction={extraction}" if extraction else None))
        elif tag == "images":
            for image in child:
                section = f"Image {image.get('key') or '?'}"
                rows += _attribute_rows(section, image)
                for metadata in image:
                    for item in metadata:
                        rows.append((section, item.get("name") or _local(item.tag), _text(item),
                                     f"section={metadata.get('section')}" if metadata.get("section") else None))
        elif tag:
            rows += _generic_rows(tag, child)
    return rows


def write_report_info(path: Path, out_dir: Path, base: str, trace: dict[str, str]) -> tuple[Path, int]:
    """Write ``<base>_report_info.csv`` (UTF-8 with BOM, like every dump CSV); return (path, rows)."""
    rows = read_report_info(path)
    target = Path(out_dir) / f"{base}{REPORT_INFO_SUFFIX}"
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(REPORT_INFO_COLUMNS)
        for row in rows:
            writer.writerow(list(row) + [trace[column] for column in TRACE_COLUMNS])
    log.info(f"Wrote {len(rows)} report header value(s) to {target}")
    return target, len(rows)

