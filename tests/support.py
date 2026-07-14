"""Shared test helpers.

Defines:    ``build_rows`` — the flat-frame convenience the production ``build_rows`` facade
            used to provide before Phase C deleted it. It assembles one source into a throwaway
            ``RunStore`` and projects it back to the flat frame, so the assembly-engine unit
            tests can keep asserting on a DataFrame without each re-implementing the store dance.
Used by:    tests.transforms.test_assemble / test_dst / test_local_zone, tests.test_diagnostics.
Depends on: model_atlas.model.store (RunStore + PASSTHROUGH_PREFIX), model_atlas.transforms.assemble.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from model_atlas.model.store import PASSTHROUGH_PREFIX, RunStore
from model_atlas.presets.spec import InputSelector, PresetSpec
from model_atlas.transforms.assemble import BuildEnv, build_assertions


def build_rows(
    records: list[dict[str, Any]],
    preset: PresetSpec,
    env: BuildEnv,
    *,
    selector: InputSelector | None = None,
    fields: list[str] | None = None,
    include_source_columns: bool = True,
    source_column_prefix: str = PASSTHROUGH_PREFIX,
) -> tuple[pd.DataFrame, list[str]]:
    """Assemble one source into a fresh ``RunStore`` and return ``(flat_frame, warnings)``.

    The single-source projection the production pipeline now performs run-wide over one shared
    store; here each call gets its own store, mirroring the old per-source facade exactly.
    """
    store = RunStore()
    warnings, _count = build_assertions(
        records, preset, env, store, selector=selector, fields=fields)
    frame = store.to_flat_frame(
        include_source_columns=include_source_columns, source_column_prefix=source_column_prefix)
    return frame, warnings
