"""Flatten one decoded ``<model>`` element into level-tagged records.

Defines:    FlatRecord (one emitted row) and flatten_model (recursive level logic).
Used by:    reader (per top-level model) and the tests.
Depends on: const (tag names + levels), lxml element API.

This preserves the legacy UFEDParser decomposition, made single-pass:
  - level 0: the top-level model  (id -> ``uuid``)
  - level 1: a ``modelField`` (1:1) or ``multiModelField`` (1:many) child model
  - level 2: the same one level deeper (a grandchild model)
A child carries its own id (``sub-uuid``) and its immediate parent's id (``main-uuid``);
``top_type`` is the level-0 model type, used for relation tracking and ``--models``
filtering. Only direct ``<field>`` children populate a record's own columns, so each
nested model's fields stay on its own row (never flattened into the parent).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from lxml import etree

from ufdr_parser.const import (
    LEVEL_SUBSUB,
    LEVEL_TOP,
    LOCAL_MODEL,
    LOCAL_MODEL_FIELD,
    LOCAL_MULTI_MODEL_FIELD,
    TAG_VALUE,
)


@dataclass(frozen=True)
class FlatRecord:
    """One flattened model instance destined for a per-(level, type) CSV."""

    level: int
    model_type: str
    top_type: str
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
    """The text of a ``<field>``'s direct ``<value>``; None for ``<empty/>`` or missing."""
    value = field_elem.find(TAG_VALUE)
    return value.text if value is not None else None


def flatten_model(
    elem: "etree._Element",
    *,
    level: int = LEVEL_TOP,
    parent_id: str | None = None,
    top_type: str | None = None,
) -> Iterator[FlatRecord]:
    """Yield the record for ``elem`` then, depth-first, its child/grandchild models."""
    model_type = elem.get("type") or ""
    model_id = elem.get("id")
    effective_top = top_type if top_type is not None else model_type

    fields: dict[str, str | None] = {}
    child_models: list["etree._Element"] = []
    for child in elem:
        local = _local(child.tag)
        if local == "field":
            name = child.get("name")
            if name is not None:
                fields[name] = _field_value(child)
        elif local in (LOCAL_MODEL_FIELD, LOCAL_MULTI_MODEL_FIELD):
            child_models.extend(
                sub for sub in child if _local(sub.tag) == LOCAL_MODEL
            )

    yield FlatRecord(
        level=level,
        model_type=model_type,
        top_type=effective_top,
        model_id=model_id,
        parent_id=parent_id,
        fields=fields,
    )

    # Recurse only down to the grandchild level (parity with the legacy three-level
    # decomposition); deeper nesting is left on the parent's row by omission.
    if level < LEVEL_SUBSUB:
        for sub in child_models:
            yield from flatten_model(
                sub, level=level + 1, parent_id=model_id, top_type=effective_top
            )
