"""Combined-workbook brick (see brick.py): registers itself on import."""
from __future__ import annotations

from ufdr_parser.bricks.base import register
from ufdr_parser.bricks.workbook.brick import WorkbookBrick, build_workbook

register(WorkbookBrick())

__all__ = ["WorkbookBrick", "build_workbook"]
