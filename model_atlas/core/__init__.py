"""Core declarations for the mATLAS run database (the 21-table relational model).

Defines:    the ``core`` package — currently the single ``vocabularies`` module,
            the one place every controlled vocabulary, family dataclass, and
            column-typing helper is declared.
Used by:    every later derivation (schema DDL/CHECKs, casts, unit conversion,
            the flat column list, the T16 verbalizer).
Depends on: the standard library only.
"""
