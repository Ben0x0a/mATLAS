"""Flatten one decoded ``<model>`` element into level-tagged records.

Defines:    FlatRecord (one emitted row) and flatten_model (recursive level logic).
Used by:    reader (per top-level model) and the tests.
Depends on: const (tag names + levels), lxml element API.

This preserves the legacy UFEDParser decomposition, made single-pass:
  - level 0: the top-level model  (id -> ``uuid``)
  - level 1: a ``modelField`` (1:1) or ``multiModelField`` (1:many) child model
  - level 2: the same one level deeper (a grandchild model)
A child carries its own id (``sub-uuid``), its immediate parent's id (``main-uuid``) and the
id of the level-0 model at the root of its tree (``top_id``); ``top_type`` is the level-0
model type, used for relation tracking and ``--models`` filtering. ``top_id`` lets a nested
row inherit its top-level model's source entry (``<extraInfos>`` is keyed by top-level ids).
Only direct ``<field>`` children populate a record's own columns, so each nested model's
fields stay on its own row (never flattened into the parent). A TimeStamp field is followed
by a ``<name>.offset`` column stating its UTC offset (see const.TIMESTAMP_OFFSET_SUFFIX).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from lxml import etree

from ufdr_parser.const import (
    LEVEL_SUBSUB,
    LEVEL_TOP,
    LOCAL_MODEL,
    LOCAL_MODEL_FIELD,
    LOCAL_MULTI_MODEL_FIELD,
    MULTI_VALUE_SEPARATOR,
    TAG_VALUE,
    TIMESTAMP_OFFSET_SUFFIX,
    TIMESTAMP_OFFSET_UNSTATED,
    TIMESTAMP_VALUE_TYPE,
)

# A trailing ISO-8601 zone designator: ``Z`` or ``+HH:MM`` / ``-HHMM``.
_ISO_OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


@dataclass(frozen=True)
class FlatRecord:
    """One flattened model instance destined for a per-(level, type) CSV."""

    level: int
    model_type: str
    top_type: str
    top_id: str | None      # id of the level-0 model at the root of this record's tree
    model_id: str | None
    parent_id: str | None
    # field name -> scalar value (None for an <empty/> field). mutable default would be
    # shared across instances if = {} were used, so build a fresh dict per record.
    fields: dict[str, str | None] = field(default_factory=dict)


def _local(tag: object) -> str | None:
    """Local name of an element tag, or None for comments / processing instructions."""
    if not isinstance(tag, str):
        return None
    return tag.rpartition("}")[2]


def _field_value(field_elem: "etree._Element") -> str | None:
    """Join every ``<value>`` of a ``<field>`` into one cell; None for ``<empty/>``/missing.

    HOW: gather each direct ``<value>``'s full text with ``itertext`` (so text split by an
    inline child element is not truncated), then join the values with MULTI_VALUE_SEPARATOR.
    WHY: a field can carry several ``<value>`` children; returning only the first silently
    dropped the rest, losing evidence with no trace. A single-value field is unchanged.
    """
    values = field_elem.findall(TAG_VALUE)
    if not values:
        return None
    return MULTI_VALUE_SEPARATOR.join("".join(value.itertext()) for value in values)


def _timestamp_offset(field_elem: "etree._Element") -> str | None:
    """The stated UTC offset of each TimeStamp ``<value>``, joined like ``_field_value``.

    Returns None when the field holds no TimeStamp value (so no offset column is added). Each
    value contributes its trailing zone designator verbatim, or TIMESTAMP_OFFSET_UNSTATED when
    it has none — never a guessed offset.
    """
    values = [
        value for value in field_elem.findall(TAG_VALUE)
        if value.get("type") == TIMESTAMP_VALUE_TYPE
    ]
    if not values:
        return None
    offsets = []
    for value in values:
        match = _ISO_OFFSET.search("".join(value.itertext()).strip())
        offsets.append(match.group(1) if match else TIMESTAMP_OFFSET_UNSTATED)
    return MULTI_VALUE_SEPARATOR.join(offsets)


def flatten_model(
    elem: "etree._Element",
    *,
    level: int = LEVEL_TOP,
    parent_id: str | None = None,
    top_type: str | None = None,
    top_id: str | None = None,
) -> Iterator[FlatRecord]:
    """Yield the record for ``elem`` then, depth-first, its child/grandchild models."""
    model_type = elem.get("type") or ""
    model_id = elem.get("id")
    effective_top = top_type if top_type is not None else model_type
    # At the top level the model IS its own tree root; children inherit the same top id so
    # every nested row can resolve the source keyed under the top-level model.
    effective_top_id = top_id if top_id is not None else model_id

    fields: dict[str, str | None] = {}
    child_models: list["etree._Element"] = []
    for child in elem:
        local = _local(child.tag)
        if local == "field":
            name = child.get("name")
            if name is not None:
                fields[name] = _field_value(child)
                offset = _timestamp_offset(child)
                if offset is not None:
                    fields[f"{name}{TIMESTAMP_OFFSET_SUFFIX}"] = offset
        elif local in (LOCAL_MODEL_FIELD, LOCAL_MULTI_MODEL_FIELD):
            child_models.extend(
                sub for sub in child if _local(sub.tag) == LOCAL_MODEL
            )

    yield FlatRecord(
        level=level,
        model_type=model_type,
        top_type=effective_top,
        top_id=effective_top_id,
        model_id=model_id,
        parent_id=parent_id,
        fields=fields,
    )

    # Recurse only down to the grandchild level (parity with the legacy three-level
    # decomposition); deeper nesting is left on the parent's row by omission.
    if level < LEVEL_SUBSUB:
        for sub in child_models:
            yield from flatten_model(
                sub,
                level=level + 1,
                parent_id=model_id,
                top_type=effective_top,
                top_id=effective_top_id,
            )
