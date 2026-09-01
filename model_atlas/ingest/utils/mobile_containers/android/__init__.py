"""Android container resolution: the fixed per-user storage layouts.

Defines:    nothing itself — a namespace for ``resolver``, which enumerates the
            credential-encrypted, device-encrypted and external-storage locations a
            package owns, each labelled with its kind and its user id.
Used by:    ``mobile_containers`` (re-exports ``AndroidResolver``).
Depends on: ``mobile_containers.models`` / ``.categories`` / ``.openers`` and the
            standard library only.
"""
