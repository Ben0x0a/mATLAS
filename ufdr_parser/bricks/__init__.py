"""Post-dump bricks: derived artefacts built from the raw per-model CSVs.

Importing this package registers every brick (each brick module calls ``register`` at import),
in the order below — which is the order they run in. To add a brick: write its module under
``ufdr_parser/bricks/`` implementing ``base.Brick`` and import it here.
"""
from __future__ import annotations

from ufdr_parser.bricks import workbook  # noqa: F401 - imported for its registration side effect
from ufdr_parser.bricks.base import (
    BrickResult,
    DumpOutput,
    registered_bricks,
    run_bricks,
    validate_bricks,
)

__all__ = ["BrickResult", "DumpOutput", "registered_bricks", "run_bricks", "validate_bricks"]
