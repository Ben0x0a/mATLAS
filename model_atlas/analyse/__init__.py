"""The L3 analysis bricks: method-stamped verdicts ABOUT observations, never inside them.

One package per analysis (``concomitance``, ``untangle``, …) plus ``helpers`` for the code
they share. The rule the layout exists to enforce: **no analysis imports another** — an
analysis may import ``analyse.helpers``, ``model_atlas.core`` and the standard library, and
nothing else from this package. ``tests/analyse/test_import_direction.py`` asserts it.
"""
