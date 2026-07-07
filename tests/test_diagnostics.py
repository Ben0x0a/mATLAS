"""Tests for the forensic crash-report handler (model_atlas.diagnostics).

Covers the value summariser's size-limiting + redaction, and end-to-end report writing:
a real exception raised through a small call stack yields a JSON artefact carrying the
frame locals, metadata and traceback — without ever dumping a whole DataFrame.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from model_atlas import config as config_module
from model_atlas.config import DiagnosticsSettings, Settings
from model_atlas.diagnostics import _summarise, write_crash_report

_CFG = DiagnosticsSettings()


def _use_diagnostics(cfg: DiagnosticsSettings) -> None:
    """Point the process-wide settings at ``cfg`` so write_crash_report (which reads the global
    config, not a passed-in object) honours the test's crash_dir / enabled flag."""
    config_module.set_settings(Settings(diagnostics=cfg))


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    config_module.reset_settings()


# --- the summariser ---------------------------------------------------------

def test_scalars_pass_through_verbatim() -> None:
    assert _summarise("n", 42, _CFG, 3) == 42
    assert _summarise("f", 3.5, _CFG, 3) == 3.5
    assert _summarise("b", True, _CFG, 3) is True
    assert _summarise("z", None, _CFG, 3) is None


def test_long_string_is_hashed_not_stored_whole() -> None:
    cfg = DiagnosticsSettings(max_string_len=10)
    summary = _summarise("blob", "x" * 5000, cfg, 3)
    assert summary["type"] == "str"
    assert summary["len"] == 5000
    assert summary["truncated"] is True
    assert len(summary["preview"]) == 10
    assert len(summary["sha256"]) == 64            # full value recoverable-by-hash, not by text


def test_redacted_name_hides_value() -> None:
    summary = _summarise("db_password", "hunter2", _CFG, 3)
    assert summary["redacted"] is True
    assert "hunter2" not in json.dumps(summary)     # plaintext never present
    assert len(summary["sha256"]) == 64             # but still correlatable


def test_dataframe_summarised_not_dumped() -> None:
    frame = pd.DataFrame({"a": range(1000), "b": ["v"] * 1000})
    summary = _summarise("frame", frame, _CFG, 3)
    assert summary["shape"] == [1000, 2]
    assert summary["columns"] == ["a", "b"]
    assert set(summary["dtypes"]) == {"a", "b"}
    assert len(summary["head"]) == _CFG.max_head     # only a tiny head, never all 1000 rows


def test_collection_item_cap_and_depth() -> None:
    cfg = DiagnosticsSettings(max_items=5)
    summary = _summarise("xs", list(range(100)), cfg, 3)
    assert summary["len"] == 100
    assert len(summary["items"]) == 5
    assert summary["truncated"] is True


def test_broken_repr_does_not_crash() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    summary = _summarise("obj", Hostile(), _CFG, 3)
    assert "repr_error" in summary                   # captured, never propagated


def test_path_value_keeps_only_type_and_size(tmp_path: Path) -> None:
    evidence = tmp_path / "sensitive_case_dir" / "acquisition.zip"
    evidence.parent.mkdir()
    evidence.write_bytes(b"data")
    summary = _summarise("input_path", evidence, _CFG, 3)
    assert summary["size_bytes"] == 4                # from stat, not a hash
    assert summary["redacted_path"] is True
    assert "name" not in summary                     # basename dropped for input files
    dumped = json.dumps(summary)
    assert "acquisition" not in dumped and "sensitive_case_dir" not in dumped  # name + dir gone


def test_string_path_under_home_is_anonymised() -> None:
    home_path = str(Path.home() / "Documents" / "case" / "phone.txt")
    summary = _summarise("source_file_path", home_path, _CFG, 3)
    assert str(Path.home()) not in summary          # username root stripped
    assert summary.startswith("~")


def test_bytes_hashed_with_hex_preview() -> None:
    summary = _summarise("raw", b"\x00\x01\x02" * 100, _CFG, 3)
    assert summary["len"] == 300
    assert summary["truncated"] is True
    assert len(summary["sha256"]) == 64


# --- end-to-end report ------------------------------------------------------

def _raise_through_a_stack(payload: pd.DataFrame) -> None:
    secret_token = "s3cr3t-value"                    # redacted by name in the report
    marker_local = "CANARY-123"                      # ordinary local, must appear
    raise ValueError("deliberate failure")


def test_write_crash_report_captures_frames_and_metadata(tmp_path: Path) -> None:
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "crash")))
    frame = pd.DataFrame({"lat": [1.0], "lon": [2.0]})
    try:
        _raise_through_a_stack(frame)
    except ValueError as exc:
        path = write_crash_report(type(exc), exc, exc.__traceback__, context={"entrypoint": "test"})

    assert path is not None and path.exists()
    assert ":" not in path.name                      # Windows-safe filename (no colon)
    report = json.loads(path.read_text(encoding="utf-8-sig"))

    assert report["exception"]["type"] == "ValueError"
    assert "deliberate failure" in report["traceback"]

    # The raising frame's locals are captured: an ordinary local verbatim, the secret redacted,
    # and the DataFrame argument summarised (not dumped).
    raising = next(f for f in report["frames"] if f["function"] == "_raise_through_a_stack")
    locals_ = raising["locals"]
    assert locals_["marker_local"] == "CANARY-123"
    assert locals_["secret_token"]["redacted"] is True
    assert "s3cr3t-value" not in json.dumps(report)
    assert locals_["payload"]["shape"] == [1, 2]

    md = report["metadata"]
    assert md["tool"] == "mATLAS"
    assert md["context"] == {"entrypoint": "test"}
    assert "python_version" in md and "platform" in md


def test_disabled_writes_nothing(tmp_path: Path) -> None:
    _use_diagnostics(DiagnosticsSettings(enabled=False, crash_dir=str(tmp_path / "crash")))
    try:
        raise RuntimeError("x")
    except RuntimeError as exc:
        path = write_crash_report(type(exc), exc, exc.__traceback__)
    assert path is None
    assert not (tmp_path / "crash").exists()


# --- breadcrumb: which record failed ---------------------------------------

def test_build_rows_breadcrumb_names_failing_record() -> None:
    """A transform that raises inside build_rows must annotate the exception with the source
    record locator (table#line + columns) so the report names WHICH row failed."""
    import yaml

    from model_atlas.presets.spec import preset_spec_from_yaml
    from model_atlas.transforms.assemble import BuildEnv, build_rows

    preset_yaml = """
preset: {id: t.crumb, name: Crumb, version: 1.0, tier: secondary}
input_selector: {format: csv, name: x.csv}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: field(lat), longitude_wgs84: field(lon)}
    time: {instant: field(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""
    spec = preset_spec_from_yaml(yaml.safe_load(preset_yaml), Path("t.yaml"))
    env = BuildEnv(linked_entity="subj")
    # A non-numeric epoch value makes the epoch decoder raise for the second record.
    records = [
        {"lat": "1", "lon": "2", "ts": "1700000000"},
        {"lat": "3", "lon": "4", "ts": "not-a-number"},
    ]
    with pytest.raises(Exception) as excinfo:
        build_rows(records, spec, env, fields=["lat", "lon", "ts"])
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("source record" in note and "#2" in note for note in notes)
