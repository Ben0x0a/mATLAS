"""``python -m ufdr_parser`` entry point — delegates to the CLI.

Used by:    end users running the module directly.
Depends on: cli (main).
"""
from __future__ import annotations

from ufdr_parser.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
