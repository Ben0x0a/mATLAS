"""Turn one decoded ``<model>`` element into one flat record, with nothing left out.

Defines:    FlatRecord (one emitted row) and build_record (one model's own row).
Used by:    reader (at each model's end tag) and the tests.
Depends on: const (names + levels), lxml element API.

THE RULE is generic on purpose: every attribute of the model and every child that is not a
nested model becomes a column, whatever the element is called. WHY not a list of known tags: the
earlier reader took ``<field>`` only, and so silently lost whatever else the report held
(``deleted_state`` and ``iscarved`` on every model, ``multiField``, ``jumptargets``, ``nodeField``,
``dataField``); a renamed or new element would have been lost the same way. Columns:

- a model attribute -> ``<attribute>`` (``deleted_state``, ``iscarved``...), first in the row;
- a child -> ``<name attribute, else its tag>``, its sub-elements' text joined with
  MULTI_VALUE_SEPARATOR (``<empty/>`` gives a blank); a repeated name -> ``<name>#2``, #3...;
- any other attribute of the child or its sub-elements -> ``<column>.<attribute>``
  (``TimeStamp.format``, ``jumptargets.targetType``), positions kept when there are several;
- a TimeStamp value -> ``<column>.offset``, the UTC offset it states, blank if none.

A nested model is its own record, built at its own end tag (see reader): it is never flattened
into its parent, and a child holding models contributes no column to the parent. A nested record
carries its id (``sub-uuid``), its immediate parent's id (``main-uuid``), the id and type of the
top-level model of its tree, and the parent's element it sits in (``parent_field``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from ufdr_parser.const import (
    FIELD_NAMING_ATTRIBUTES,
    LEVEL_TOP,
    LOCAL_MODEL,
    MODEL_CONTAINER_TAGS,
    MODEL_IDENTITY_ATTRIBUTES,
    MULTI_VALUE_SEPARATOR,
    PARENT_FIELD_COLNAME,
    TIMESTAMP_OFFSET_SUFFIX,
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
    # column name -> value (None for an <empty/> field). mutable default would be shared across
    # instances if = {} were used, so build a fresh dict per record.
    fields: dict[str, str | None] = field(default_factory=dict)
    # Whether a child (not just a model attribute) gave a value: the integrity check's evidence.
    has_content: bool = True


def _local(tag: object) -> str | None:
    """Local name of an element tag, or None for comments / processing instructions."""
    if not isinstance(tag, str):
        return None
    return tag.rpartition("}")[2]


def _put(fields: dict[str, str | None], column: str, value: str | None) -> str:
    """Store a value under ``column``, or ``column#2``, #3... if taken; return the key used.

    WHY: a model can repeat a field name; keeping only the last would lose the others silently.
    """
    key, index = column, 2
    while key in fields:
        key, index = f"{column}#{index}", index + 1
    fields[key] = value
    return key


def _add_child(fields: dict[str, str | None], child: "etree._Element", name: str) -> bool:
    """Add one non-model child's columns; return whether it held a value."""
    parts = [sub for sub in child if isinstance(sub.tag, str) and _local(sub.tag) != "empty"]
    if parts:
        value = MULTI_VALUE_SEPARATOR.join("".join(part.itertext()) for part in parts)
    else:
        value = (child.text or "").strip() or None
    column = _put(fields, name, value)
    for attribute, text in child.attrib.items():
        if attribute not in FIELD_NAMING_ATTRIBUTES:
            _put(fields, f"{column}.{attribute}", text)
    names = {name_ for part in parts for name_ in part.attrib if name_ not in FIELD_NAMING_ATTRIBUTES}
    for attribute in sorted(names):
        fields[f"{column}.{attribute}"] = MULTI_VALUE_SEPARATOR.join(part.get(attribute, "") for part in parts)
    if any(part.get("type") == TIMESTAMP_VALUE_TYPE for part in parts):
        offsets = []
        for part in parts:
            match = _ISO_OFFSET.search("".join(part.itertext()).strip())
            offsets.append(match.group(1) if match else "")
        # The column exists for every time field; blank, never a default, when none is stated.
        fields[f"{column}{TIMESTAMP_OFFSET_SUFFIX}"] = MULTI_VALUE_SEPARATOR.join(offsets) if any(offsets) else None
    return bool(value)


def build_record(
    elem: "etree._Element",
    *,
    level: int = LEVEL_TOP,
    parent_id: str | None = None,
    top_type: str | None = None,
    top_id: str | None = None,
    parent_field: str | None = None,
) -> FlatRecord:
    """The record for ``elem`` alone: its attributes and its non-model children."""
    model_type = elem.get("type") or ""
    model_id = elem.get("id")
    fields: dict[str, str | None] = {}
    if parent_field is not None:
        fields[PARENT_FIELD_COLNAME] = parent_field
    for attribute, value in elem.attrib.items():
        if attribute not in MODEL_IDENTITY_ATTRIBUTES:
            _put(fields, attribute, value)
    has_content = False
    for child in elem:
        local = _local(child.tag)
        if local is None or local in MODEL_CONTAINER_TAGS:
            continue
        if any(_local(sub.tag) == LOCAL_MODEL for sub in child):
            continue  # holds nested models (an unknown container): they are their own records
        has_content |= _add_child(fields, child, child.get("name") or local)
    return FlatRecord(
        level=level,
        model_type=model_type,
        # At the top level the model IS its own tree root; children inherit the same top id.
        top_type=top_type if top_type is not None else model_type,
        top_id=top_id if top_id is not None else model_id,
        model_id=model_id,
        parent_id=parent_id,
        fields=fields,
        has_content=has_content,
    )
