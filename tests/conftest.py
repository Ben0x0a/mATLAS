"""Keep tests runnable from a checkout without installing the package."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from model_atlas.config import Settings, reset_settings, set_settings


@pytest.fixture(autouse=True)
def _isolated_settings():
    """Force built-in default settings for every test, so results never depend on a
    developer's local matlas_config.toml (e.g. a configured local_zone). Tests that need a
    specific zone set it explicitly (BuildEnv.local_zone / process(local_zone=...))."""
    set_settings(Settings())
    yield
    reset_settings()
