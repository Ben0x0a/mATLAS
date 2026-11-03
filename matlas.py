"""Clone-and-run entry point for Model Atlas.

Keep this file intentionally tiny: it only dispatches to the launcher package.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        return _dispatch(args)
    except ModuleNotFoundError as exc:
        # A dependency missing at import time would otherwise surface as a raw traceback that
        # never says how to install it. launcher.install_help imports nothing of ours.
        from launcher.install_help import missing_package_message

        message = missing_package_message(exc.name)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 1


def _dispatch(args: list[str]) -> int:
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
