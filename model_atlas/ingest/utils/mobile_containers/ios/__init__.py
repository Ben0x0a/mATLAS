"""iOS container resolution: the metadata-plist index joined to references.sqlite3.

Defines:    nothing itself — a namespace for ``identifiers`` (the one matching rule),
            ``metadata`` (source 1: every container's own plist), ``references``
            (source 2: MobileContainerManager's database) and ``resolver`` (the join).
Used by:    ``mobile_containers`` (re-exports ``IosResolver``).
Depends on: ``mobile_containers.models`` / ``.categories`` / ``.openers`` and the
            standard library only.
"""
