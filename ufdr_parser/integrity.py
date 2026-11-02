"""Detect a report the dumper did not fully read, instead of writing partial output quietly.

Defines:    ReportIntegrityError, ParseAudit (what the parser saw), check_parse (the checks that
            stop the run) and audit_findings (those that do not).
Used by:    reader (fills the audit), parallel (merges the workers' audits), dump (runs the
            checks after the CSVs are written).
Depends on: models (FlatRecord), standard library only.

WHY: the parser is deliberately tolerant (``recover=True``, because real reports contain invalid
XML characters) and generic, so nothing in it raises when data goes missing. These checks compare
what was read with what the report holds, by means independent of the parser.

Two severities. When the report as a whole was not read (``check_parse``: no ``<model>`` found,
a truncated file) the output cannot be trusted: the run stops with an error and no workbook is
built; the CSVs written so far stay, as they are all there is. When a part was lost
(``audit_findings``: models discarded by the parser on invalid XML, counted against the
``<model>`` tags in the raw bytes) the rest is sound, so the output is kept and the error raised
next to it. Sections the dumper does not read are reported as omissions.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ufdr_parser.models import FlatRecord

_ROOT_CLOSE = b"</project>"


class ReportIntegrityError(Exception):
    """The report was not understood well enough to trust the output."""


def ends_like_a_report(tail: bytes) -> bool:
    """Whether the report's last bytes close its root element (else: a truncated file)."""
    return tail.rstrip().endswith(_ROOT_CLOSE)


@dataclass
class ParseAudit:
    """What the parser saw, compared with the bytes it was given."""

    records: int = 0
    records_with_content: int = 0
    # <model> start tags counted in the raw bytes, independently of the parser.
    model_tags: int = 0
    # Top-level section -> elements it holds, for sections the dumper does not read.
    unread_sections: Counter = field(default_factory=Counter)  # mutable default — one per audit

    def observe(self, record: FlatRecord) -> None:
        self.records += 1
        if record.has_content:
            self.records_with_content += 1

    def merge(self, other: "ParseAudit") -> None:
        self.records += other.records
        self.records_with_content += other.records_with_content
        self.model_tags += other.model_tags
        self.unread_sections.update(other.unread_sections)


def check_parse(audit: ParseAudit, *, complete: bool, declared_models: int | None, filtered: bool) -> None:
    """Raise ReportIntegrityError when the report as a whole was not read.

    ``declared_models`` is the header's ModelCount (None if absent); only an explicit 0 makes an
    empty result legitimate. ``filtered`` is a ``--models`` run: it can legitimately parse nothing.
    """
    hint = "The report's structure may have changed (e.g. a new Cellebrite version)."
    if not complete:
        raise ReportIntegrityError(
            "The report does not end properly: the file is truncated or incomplete. "
            f"Only {audit.records} record(s) were read; the CSVs hold just those and no workbook "
            "was built."
        )
    if filtered:
        return
    if audit.records == 0:
        if declared_models == 0:
            return
        declared = f"declares {declared_models} model(s)" if declared_models else "does not declare itself empty"
        raise ReportIntegrityError(f"No <model> element could be read, although the report {declared}. {hint}")
    if audit.records_with_content == 0:
        raise ReportIntegrityError(f"{audit.records} record(s) were found but none holds any value. {hint}")


def audit_findings(audit: ParseAudit) -> tuple[list[str], list[str]]:
    """``(errors, omissions)`` from an audit: data lost, and sections knowingly not read."""
    errors: list[str] = []
    lost = audit.model_tags - audit.records
    if lost > 0:
        # recover=True discards what it cannot parse without raising; only the raw count shows it.
        errors.append(
            f"{lost} of the report's {audit.model_tags} model(s) were lost by the XML parser: the "
            "report contains invalid XML around them, and they are in neither the CSVs nor the workbook."
        )
    omissions = [
        f"The report's <{section}> section holds {count} element(s), which the dump does not read."
        for section, count in sorted(audit.unread_sections.items())
    ]
    return errors, omissions
