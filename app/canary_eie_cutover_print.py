"""CANARY (DO NOT MERGE): EIE org-CI cutover H6, witness for the T20 print ban in `make check` (#384).

Service code may not print, and a `noqa` suppression must not hide it (`--ignore-noqa`).
"""


def report(value: int) -> None:
    print(value)  # noqa: T201
