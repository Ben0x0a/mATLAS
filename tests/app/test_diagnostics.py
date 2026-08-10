"""Tests for the forensic crash-report handler (model_atlas.app.diagnostics).

Covers the value summariser's size-limiting + redaction, and end-to-end report writing:
a real exception raised through a small call stack yields a JSON + Markdown artefact pair,
split into a safe (shareable) and a sensitive (review-before-sharing) section, carrying the
frame locals, metadata and traceback — without ever dumping a whole DataFrame.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pandas as pd
import pytest

from model_atlas.app import config as config_module
from model_atlas.app.config import DiagnosticsSettings, Settings
from model_atlas.app.diagnostics import (
    _relativise_path,
    _render_markdown,
    KIND_BUG,
    KIND_ERROR,
    _summarise,
    capture_exception,
    error_report_count,
    write_bug_report,
    write_crash_report,
)

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
    safe, sensitive = report["safe"], report["sensitive"]

    # The exception TYPE is safe; its message + traceback are run-time data (sensitive).
    assert safe["exception_type"] == "ValueError"
    assert "deliberate failure" in sensitive["traceback"]

    # The raising frame's locals are captured in the SENSITIVE section: an ordinary local
    # verbatim, the secret redacted, and the DataFrame argument summarised (not dumped).
    raising = next(f for f in sensitive["frames"] if f["function"] == "_raise_through_a_stack")
    locals_ = raising["locals"]
    assert locals_["marker_local"] == "CANARY-123"
    assert locals_["secret_token"]["redacted"] is True
    assert "s3cr3t-value" not in json.dumps(report)
    assert locals_["payload"]["shape"] == [1, 2]

    # The safe stack carries only WHERE the code was, never the locals values.
    safe_raising = next(f for f in safe["frames"] if f["function"] == "_raise_through_a_stack")
    assert set(safe_raising) == {"file", "lineno", "function", "code"}
    assert "CANARY-123" not in json.dumps(safe)

    md = safe["metadata"]
    assert md["tool"] == "mATLAS"
    assert safe["context"] == {"entrypoint": "test"}
    assert "python_version" in md and "platform" in md


def test_project_paths_anonymise_relative_to_the_repo_root() -> None:
    # Defect 1: the anchor is the REPO ROOT, so a project file reads as "model_atlas/…", not
    # "app/…" or "ingest/…". diagnostics.py itself is <repo>/model_atlas/app/diagnostics.py.
    from model_atlas.app import diagnostics

    repo_root = Path(diagnostics.__file__).resolve().parents[2]
    project_file = str(repo_root / "model_atlas" / "app" / "pipeline.py")
    assert _relativise_path(project_file, _CFG, basename_fallback=True) == "model_atlas/app/pipeline.py"


def test_markdown_report_is_written_and_leaks_no_secret(tmp_path: Path) -> None:
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "crash")))
    try:
        _raise_through_a_stack(pd.DataFrame({"lat": [1.0]}))
    except ValueError as exc:
        json_path = write_crash_report(type(exc), exc, exc.__traceback__)

    # The human Markdown sits beside the JSON, same stem.
    md_path = json_path.with_suffix(".md")
    assert md_path.exists()
    text = md_path.read_text(encoding="utf-8-sig")
    assert text.startswith("# mATLAS crash report")
    assert "## Safe (shareable)" in text and "## Sensitive (review before sharing)" in text
    assert "`ValueError`" in text
    # The secret plaintext appears in NEITHER artefact (redaction survives rendering).
    assert "s3cr3t-value" not in text
    assert "s3cr3t-value" not in json_path.read_text(encoding="utf-8-sig")


def test_disabled_writes_nothing(tmp_path: Path) -> None:
    _use_diagnostics(DiagnosticsSettings(enabled=False, crash_dir=str(tmp_path / "crash")))
    try:
        raise RuntimeError("x")
    except RuntimeError as exc:
        path = write_crash_report(type(exc), exc, exc.__traceback__)
    assert path is None
    assert not (tmp_path / "crash").exists()


# --- summariser type-dispatch branches (must never dump/leak, never crash) ----

class _Thing:
    def __init__(self) -> None:
        self.visible = "shown"
        self.password = "s3cr3t-attr"


def test_mapping_is_summarised_with_nested_key_redaction() -> None:
    summary = _summarise("payload", {"note": "hello", "password": "s3cr3t-map"}, _CFG, 3)
    assert summary["type"] == "dict" and summary["len"] == 2
    assert summary["items"]["note"] == "hello"
    assert summary["items"]["password"]["redacted"] is True   # redacted by the KEY name
    assert "s3cr3t-map" not in json.dumps(summary)


def test_sequence_is_capped_at_max_items() -> None:
    summary = _summarise("nums", list(range(50)), _CFG, 3)
    assert summary["type"] == "list" and summary["len"] == 50
    assert len(summary["items"]) == _CFG.max_items
    assert summary["truncated"] is True


def test_object_attrs_are_summarised_and_redacted() -> None:
    summary = _summarise("obj", _Thing(), _CFG, 3)
    assert summary["attrs"]["visible"] == "shown"
    assert summary["attrs"]["password"]["redacted"] is True   # redacted by the ATTR name
    assert "s3cr3t-attr" not in json.dumps(summary)


def test_deep_nesting_degrades_to_a_shallow_repr() -> None:
    # depth counts down; at depth 0 a container degrades instead of recursing forever.
    summary = _summarise("deep", {"a": {"b": {"c": 1}}}, _CFG, 1)
    assert summary["items"]["a"]["depth_exceeded"] is True


def test_series_is_summarised_head_only() -> None:
    summary = _summarise("s", pd.Series([1.0, 2.0, 3.0, 4.0]), _CFG, 3)
    assert "Series" in summary["type"]
    assert summary["len"] == 4 and isinstance(summary["head"], list)


# --- the human Markdown renderer branches -----------------------------------

def test_markdown_renders_redacted_value_and_empty_frame() -> None:
    report = {
        "schema": "matlas.crash-report/2",
        "safe": {
            "exception_type": "ValueError",
            "frames": [{"file": "model_atlas/x.py", "lineno": 3, "function": "f", "code": "boom()"}],
            "metadata": {"tool_version": "0.1.0", "python_version": "3.14"},
        },
        "sensitive": {
            "exception_message": "bad",
            "frames": [
                {"function": "f", "lineno": 3,
                 "locals": {"secret_token": {"redacted": True, "type": "str",
                                             "sha256": "abcdef1234567890"}}},
                {"function": "g", "lineno": 9, "locals": {}},
            ],
        },
    }
    md = _render_markdown(report)
    assert "`ValueError`" in md
    assert "model_atlas/x.py:3" in md
    assert "_redacted_" in md and "abcdef123456" in md   # short sha, never the plaintext
    assert "_(no locals)_" in md                          # the empty-locals frame


# --- the bug report: same artefact, no exception -----------------------------
# A bug that produces the wrong answer raises nothing, so the crash path never fires and the
# state that explains it lives only in the running frames. These cover that second half.

def test_bug_report_captures_every_live_thread(tmp_path: Path) -> None:
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "reports")))
    started, release = threading.Event(), threading.Event()

    def worker() -> None:
        worker_only_local = "state-that-explains-the-bug"   # noqa: F841 - the point of the test
        started.set()
        release.wait(timeout=5)

    thread = threading.Thread(target=worker, name="pipeline-worker")
    thread.start()
    try:
        assert started.wait(timeout=5)
        path = write_bug_report(context={"entrypoint": "test"})
        assert path is not None
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    finally:
        release.set()
        thread.join(timeout=5)

    assert report["kind"] == KIND_BUG
    assert report["safe"]["exception_type"] is None      # no exception, by definition
    names = {t["thread_name"] for t in report["safe"]["threads"]}
    assert "pipeline-worker" in names, "a worker thread's stack must be captured"
    # The locals of a thread that never raised are exactly what a silent bug leaves nowhere
    # else — capturing the stack without them would defeat the feature.
    worker_frames = next(t for t in report["sensitive"]["threads"]
                         if t["thread_name"] == "pipeline-worker")["frames"]
    captured = {name for frame in worker_frames for name in (frame.get("locals") or {})}
    assert "worker_only_local" in captured


def test_bug_report_excludes_the_reporters_own_frames(tmp_path: Path) -> None:
    # The report must describe the application, not the act of reporting.
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "reports")))
    path = write_bug_report()
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    main = next(t for t in report["safe"]["threads"] if t["is_main"])
    functions = {frame["function"] for frame in main["frames"]}
    assert "write_bug_report" not in functions
    assert "_collect_thread_stacks" not in functions
    # ... while still reaching the calling test frame.
    assert "test_bug_report_excludes_the_reporters_own_frames" in functions


def test_bug_and_error_reports_share_one_format(tmp_path: Path) -> None:
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "reports")))
    bug = json.loads(write_bug_report().read_text(encoding="utf-8-sig"))
    try:
        raise ValueError("boom")
    except ValueError as exc:
        error = json.loads(capture_exception(exc).read_text(encoding="utf-8-sig"))
    assert bug["schema"] == error["schema"]
    assert {"schema", "tool", "kind", "safe", "sensitive"} <= set(bug) & set(error)
    assert (bug["kind"], error["kind"]) == (KIND_BUG, KIND_ERROR)
    # Both render, and both carry the fileable template.
    for report in (bug, error):
        markdown = _render_markdown(report)
        assert "## Steps to reproduce" in markdown
        assert "## Expected vs actual" in markdown


def test_error_count_ignores_bug_reports(tmp_path: Path) -> None:
    """The GUI caption keys off this: filing a bug report must not claim the tool errored."""
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "reports")))
    assert error_report_count() == 0
    write_bug_report()
    assert error_report_count() == 0, "a bug report is not an error"
    try:
        raise ValueError("boom")
    except ValueError as exc:
        capture_exception(exc)
    assert error_report_count() == 1


def test_capture_exception_reports_a_swallowed_failure(tmp_path: Path) -> None:
    """The funnel sys.excepthook never sees — every GUI background task flows through it."""
    _use_diagnostics(DiagnosticsSettings(crash_dir=str(tmp_path / "reports")))
    try:
        marker_local = "visible-in-the-report"   # noqa: F841 - asserted below
        raise RuntimeError("swallowed")
    except RuntimeError as exc:
        path = capture_exception(exc, context={"op": "background task"})
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    assert report["kind"] == KIND_ERROR
    assert report["safe"]["context"] == {"op": "background task"}
    captured = {name for frame in report["sensitive"]["frames"]
                for name in (frame.get("locals") or {})}
    assert "marker_local" in captured
