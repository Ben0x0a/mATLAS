"""Concrete XML dialects, one file per forensic format.

Importing this package registers every dialect (via each module's ``register_dialect``
call) so the namespace -> dialect lookup in ``xml_reader`` is populated. Add a new format
as a sibling module that implements ``XmlDialect`` and registers itself, then import it
here.
"""
from __future__ import annotations

from model_atlas.sources.readers.xml_specifics import (  # noqa: F401 - registers
    cellebrite_xml_reports,
)

__all__ = ["cellebrite_xml_reports"]
