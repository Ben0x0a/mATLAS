"""Clone-and-run entry point for Model Atlas.

Keep this file intentionally tiny: it only dispatches to the launcher package.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("Launching GUI. For CLI help, run: python matlas.py --help")
        from launcher.gui import run

        return run([])
    if args[0] == "gui":
        from launcher.gui import run

        return run(args[1:])

    from launcher.cli import run

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
