"""One entry point for both platforms: detection, dispatch, and a symmetric contract.

Defines:    tests that ``detect_platform`` recognises each acquisition shape, that
            ``build_resolver`` returns the matching resolver and refuses an unrecognised
            layout, and that both resolvers satisfy the same ``Resolver`` surface.
Used by:    the test suite.
Depends on: the ``ios_acquisition`` / ``android_acquisition`` fixtures in this directory's
            ``conftest`` and the public surface of ``mobile_containers``.

WHY the symmetry is asserted rather than assumed: the whole point of the dispatcher is that
a caller never branches on the platform. If one resolver grows a method the other lacks —
``findings`` was exactly that, present on iOS and absent on Android — the dispatcher's
promise quietly stops holding for whichever platform is missing it, and only a caller in
production finds out.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from model_atlas.ingest.utils.mobile_containers import (
    ANDROID_PLATFORM_NAME,
    IOS_PLATFORM_NAME,
    AndroidResolver,
    IosResolver,
    UnsupportedAcquisitionError,
    ZipOpener,
    build_resolver,
    detect_platform,
)


def _unrecognised(tmp_path: Path) -> Path:
    """An archive that is plainly neither platform — a tool report bundle, say."""
    archive = tmp_path / "not_an_acquisition.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("report/report.xml", b"<report/>")
        zf.writestr("report/media/IMG_0001.JPG", b"\xff\xd8\xff")
    return archive


def test_detect_platform_recognises_ios(ios_acquisition: Path) -> None:
    with ZipOpener(ios_acquisition) as opener:
        assert detect_platform(opener) == IOS_PLATFORM_NAME


def test_detect_platform_recognises_android(android_acquisition: Path) -> None:
    with ZipOpener(android_acquisition) as opener:
        assert detect_platform(opener) == ANDROID_PLATFORM_NAME


def test_detect_platform_answers_none_for_neither(tmp_path: Path) -> None:
    """An unrecognised acquisition is an ordinary answer, not an exception."""
    with ZipOpener(_unrecognised(tmp_path)) as opener:
        assert detect_platform(opener) is None


def test_build_resolver_dispatches_to_the_right_platform(
    ios_acquisition: Path, android_acquisition: Path
) -> None:
    with ZipOpener(ios_acquisition) as opener:
        assert isinstance(build_resolver(opener), IosResolver)
    with ZipOpener(android_acquisition) as opener:
        assert isinstance(build_resolver(opener), AndroidResolver)


def test_build_resolver_refuses_an_unrecognised_acquisition(tmp_path: Path) -> None:
    """Refusing loudly beats returning a resolver that answers 'nothing' to everything."""
    with ZipOpener(_unrecognised(tmp_path)) as opener:
        with pytest.raises(UnsupportedAcquisitionError):
            build_resolver(opener)


@pytest.mark.parametrize("fixture_name", ["ios_acquisition", "android_acquisition"])
def test_both_resolvers_offer_the_same_surface(fixture_name: str, request) -> None:
    """The contract the dispatcher promises: one surface, whichever platform answered."""
    acquisition = request.getfixturevalue(fixture_name)
    with ZipOpener(acquisition) as opener:
        resolver = build_resolver(opener)
        findings = resolver.findings
        result = resolver.resolve("does.not.exist.anywhere")
    assert isinstance(findings, tuple), "findings must be a tuple on both platforms"
    assert result == [], "an unknown package resolves to nothing, not an error"
