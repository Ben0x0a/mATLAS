"""Self-contained helper libraries the ingest bricks lean on.

Defines:    nothing itself — a namespace for standalone libraries that answer one
            narrow question about an acquisition and are deliberately kept free of
            mATLAS domain types, so each can be lifted out into its own distribution.
Used by:    ``model_atlas.ingest`` (its bricks import the helpers from here).
Depends on: nothing.

Anything living under ``utils/`` is expected to be detachable: it may depend on the
standard library and on its own submodules, never on the rest of ``model_atlas``. The
constraint is enforced per-library by a test, not by convention alone.
"""
