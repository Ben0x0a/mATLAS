"""Tests for the W3C PROV-JSON traceability sidecar (model_atlas.app.reporting).

The ``traceability_format="prov"`` path (build_traceability_prov) was previously unexercised.
These cover its two branches: a run WITH an output CSV (an output entity plus used/derived
edges per source) and one WITHOUT (no output entity, no generation/derivation edges).
Used by:    pytest.
Depends on: model_atlas.app.reporting.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.app.reporting import (
    build_traceability_prov,
    build_traceability_readable,
)

_SOURCE = {
    "source_file": "cached.csv",
    "raw_source_path": "/x/Cache.sqlite",
    "input_file_path": "/in/cached.csv",
    "input_file_name": "cached.csv",
    "container_chain": [],
    "format": "csv",
    "table": None,
    "sheet": None,
    "source_fingerprint": "abc123",
    "preset_id": "golden.secondary.cached",
    "matched_preset": "Golden Secondary Cached",
    "source_tier": "secondary",
}


def test_prov_record_with_output_has_entities_used_and_derivation() -> None:
    prov = build_traceability_prov(
        started_at="2026-07-23T00:00:00+00:00",
        finished_at="2026-07-23T00:00:05+00:00",
        output_csv=Path("/out/merged.csv"),
        sources=[_SOURCE],
    )
    # The PROV skeleton is present and well-formed.
    assert set(prov["prefix"]) == {"matlas", "prov"}
    assert "matlas:run" in prov["activity"]
    # The output and the one source are both entities...
    assert prov["entity"]["matlas:output"]["prov:label"] == "/out/merged.csv"
    source_entity = prov["entity"]["matlas:source/0"]
    assert source_entity["prov:label"] == "cached.csv"
    assert source_entity["matlas:preset"] == "Golden Secondary Cached"
    # ...the source was USED, the output was GENERATED, and it was DERIVED FROM the source.
    assert prov["used"]["matlas:_u0"] == {"prov:activity": "matlas:run", "prov:entity": "matlas:source/0"}
    assert prov["wasGeneratedBy"]["matlas:_g"]["prov:entity"] == "matlas:output"
    assert prov["wasDerivedFrom"]["matlas:_d0"] == {
        "prov:generatedEntity": "matlas:output", "prov:usedEntity": "matlas:source/0"}


def test_prov_record_without_output_omits_generation_and_derivation() -> None:
    prov = build_traceability_prov(
        started_at="2026-07-23T00:00:00+00:00",
        finished_at="2026-07-23T00:00:05+00:00",
        output_csv=None, sources=[_SOURCE],
    )
    # No output entity, no generation, no derivation — but the source is still an entity used.
    assert "matlas:output" not in prov["entity"]
    assert prov["wasGeneratedBy"] == {}
    assert prov["wasDerivedFrom"] == {}
    assert prov["used"]["matlas:_u0"]["prov:entity"] == "matlas:source/0"


def test_readable_and_prov_share_one_finish_time() -> None:
    # A single run's two sidecar formats must AGREE on when the run finished: the caller
    # computes the finish time once and passes it to both builders (previously each stamped
    # its own now_iso(), so the two artefacts drifted by the gap between the calls).
    started_at = "2026-07-23T00:00:00+00:00"
    finished_at = "2026-07-23T00:00:05+00:00"
    readable = build_traceability_readable(
        started_at=started_at, finished_at=finished_at,
        input_path=Path("/in/cached.csv"), presets_path=Path("/in/presets"),
        output_csv=Path("/out/merged.csv"), row_counts={"assertions": 2},
        sources=[_SOURCE], warning_count=0,
    )
    prov = build_traceability_prov(
        started_at=started_at, finished_at=finished_at,
        output_csv=Path("/out/merged.csv"), sources=[_SOURCE],
    )
    assert readable["run"]["finished_at"] == finished_at
    assert prov["activity"]["matlas:run"]["prov:endTime"] == finished_at
    assert readable["run"]["finished_at"] == prov["activity"]["matlas:run"]["prov:endTime"]
