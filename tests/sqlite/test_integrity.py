"""Tests for the source-integrity helpers (``model_atlas.sqlite.integrity``).

This module is chain-of-custody code — it proves the tool did not modify the evidence it
read — so its two strategies are pinned here: FULL whole-file SHA-256 (snapshot /
verify_unchanged) and the STRATEGIC zip fingerprint (central-directory + per-entry byte
spans, verified without a second full pass). Both the match and the MISMATCH paths are
asserted: an integrity check that cannot detect a change is worthless.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from model_atlas.sqlite.integrity import (
    IntegrityReport,
    _entry_data_offset,
    _sha256_range,
    fingerprint_zip,
    format_report,
    sha256_file,
    snapshot,
    strategic_method_doc,
    verify_unchanged,
    verify_zip_fingerprint,
    zip_resume_key,
)


# --- FULL whole-file integrity -----------------------------------------------

def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    data = b"hello world" * 1000    # spans several read chunks
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    assert sha256_file(f) == hashlib.sha256(data).hexdigest()


def test_snapshot_skips_missing_and_none(tmp_path: Path) -> None:
    present = tmp_path / "here"
    present.write_bytes(b"x")
    snap = snapshot({"here": present, "gone": tmp_path / "nope", "none": None})  # type: ignore[dict-item]
    assert set(snap) == {"here"}


def test_verify_unchanged_all_match(tmp_path: Path) -> None:
    f = tmp_path / "db.sqlite"
    f.write_bytes(b"evidence")
    before = snapshot({"db": f})
    report = verify_unchanged(before, {"db": f})
    assert report.ok
    assert report.per_file["db"]["match"] is True


def test_verify_unchanged_detects_a_modified_file(tmp_path: Path) -> None:
    f = tmp_path / "db.sqlite"
    f.write_bytes(b"evidence")
    before = snapshot({"db": f})
    f.write_bytes(b"tampered")               # same length, different bytes
    report = verify_unchanged(before, {"db": f})
    assert not report.ok
    assert report.per_file["db"]["match"] is False
    assert report.per_file["db"]["before"] != report.per_file["db"]["after"]


def test_verify_unchanged_flags_a_file_gone_after(tmp_path: Path) -> None:
    f = tmp_path / "db.sqlite"
    f.write_bytes(b"evidence")
    before = snapshot({"db": f})
    f.unlink()
    report = verify_unchanged(before, {"db": f})
    assert not report.ok
    assert report.per_file["db"]["note"] == "missing after"
    assert report.per_file["db"]["after"] is None


def test_report_to_dict_and_format(tmp_path: Path) -> None:
    ok = IntegrityReport(ok=True, per_file={"db": {"match": True}})
    assert ok.to_dict() == {"ok": True, "per_file": {"db": {"match": True}}}
    text_ok = format_report(ok)
    assert "Integrity check: OK" in text_ok and "[OK] db" in text_ok

    bad = IntegrityReport(ok=False, per_file={"db": {"match": False, "before": "aa", "after": "bb"}})
    text_bad = format_report(bad)
    assert "Integrity check: FAILED" in text_bad
    assert "[MISMATCH] db" in text_bad
    assert "before: aa" in text_bad and "after:  bb" in text_bad


# --- STRATEGIC zip fingerprint -----------------------------------------------

def _make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _infos(path: Path, names: list[str]) -> dict[str, zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.getinfo(n) for n in names}


def test_fingerprint_zip_records_structure_and_observation_content_hash(tmp_path: Path) -> None:
    content = b"hello" * 100
    z = _make_zip(tmp_path / "a.zip", {"db.sqlite": content, "db.sqlite-wal": b"wal" * 50})
    fp = fingerprint_zip(z, _infos(z, ["db.sqlite"]))

    assert fp.algorithm == "sha256"
    assert fp.file_size == z.stat().st_size
    assert fp.cd_size == fp.file_size - fp.cd_offset
    assert len(fp.entries) == 1
    entry = fp.entries[0]
    assert entry.arcname == "db.sqlite"
    assert entry.uncompressed_size == len(content)
    # the content hash is the decompressed evidence itself
    assert entry.content_sha256 == hashlib.sha256(content).hexdigest()

    d = fp.to_dict()
    assert d["central_directory"]["offset"] == fp.cd_offset
    assert d["entries"][0]["arcname"] == "db.sqlite"


def test_verify_zip_fingerprint_passes_on_the_same_archive(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "a.zip", {"db.sqlite": b"hello" * 100})
    fp = fingerprint_zip(z, _infos(z, ["db.sqlite"]))
    report = verify_zip_fingerprint(fp, z)
    assert report.ok
    assert report.per_file["file_size"]["match"]
    assert report.per_file["central_directory"]["match"]
    assert report.per_file["db.sqlite"]["match"]


def test_verify_zip_fingerprint_detects_a_rewritten_entry(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "a.zip", {"db.sqlite": b"hello" * 100})
    fp = fingerprint_zip(z, _infos(z, ["db.sqlite"]))
    _make_zip(z, {"db.sqlite": b"TAMPERED" * 200})   # different content AND length
    report = verify_zip_fingerprint(fp, z)
    assert not report.ok
    assert not report.per_file["file_size"]["match"]


def test_resume_key_is_stable_and_change_sensitive(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "a.zip", {"db.sqlite": b"one"})
    fp = fingerprint_zip(z, _infos(z, ["db.sqlite"]))
    # the fingerprint's own key equals the standalone reader's key for the same archive
    assert fp.resume_key() == f"strategic:{fp.file_size}:{fp.cd_sha256}"
    assert fp.resume_key() == zip_resume_key(z)
    first = zip_resume_key(z)
    assert zip_resume_key(z) == first                # stable across reads
    _make_zip(z, {"db.sqlite": b"two"})
    assert zip_resume_key(z) != first                # changes when an entry changes


def test_strategic_method_doc_is_reproducible_recipe() -> None:
    doc = strategic_method_doc()
    assert isinstance(doc, str) and len(doc) > 50


# --- low-level range + local-header parsing ----------------------------------

def test_sha256_range_past_eof_hashes_only_available_bytes(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"abcdef")
    with open(p, "rb") as fh:
        got = _sha256_range(fh, 3, 100)          # ask for 100 from offset 3; only 3 exist
    assert got == hashlib.sha256(b"def").hexdigest()


def test_entry_data_offset_truncated_header_raises(tmp_path: Path) -> None:
    p = tmp_path / "short.bin"
    p.write_bytes(b"\x00" * 10)                  # fewer than the 30-byte fixed header
    with open(p, "rb") as fh:
        with pytest.raises(ValueError):
            _entry_data_offset(fh, 0)


def test_entry_data_offset_bad_signature_raises(tmp_path: Path) -> None:
    p = tmp_path / "nosig.bin"
    p.write_bytes(b"\x00" * 40)                  # 40 bytes, but no PK\x03\x04 signature
    with open(p, "rb") as fh:
        with pytest.raises(ValueError):
            _entry_data_offset(fh, 0)
