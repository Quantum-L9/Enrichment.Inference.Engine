"""WU-29: agent-check stage [3/8] is a mypy ratchet (ADR-001 / WAIVER-001)."""

from __future__ import annotations

from collections import Counter

from tools.mypy_ratchet import compare, fingerprints

_OUTPUT = """\
app/a.py:10: error: Returning Any from function declared to return "int"  [no-any-return]
app/b.py:3:5: error: Argument 1 to "f" has incompatible type "str"; expected "int"  [arg-type]
app/a.py: note: some note
Found 2 errors in 2 files (checked 2 source files)
"""


def test_fingerprints_drop_line_numbers() -> None:
    assert fingerprints(_OUTPUT) == Counter(
        {
            'app/a.py: Returning Any from function declared to return "int"  [no-any-return]': 1,
            'app/b.py: Argument 1 to "f" has incompatible type "str"; expected "int"  [arg-type]': 1,
        }
    )


def test_recorded_errors_pass() -> None:
    current = fingerprints(_OUTPUT)
    assert compare(current, Counter(current)) == ([], [])


def test_a_new_error_fails_negative_control() -> None:
    baseline = fingerprints(_OUTPUT)
    current = baseline + Counter({'app/c.py: Name "x" is not defined  [name-defined]': 1})
    new, fixed = compare(current, baseline)
    assert new == ['app/c.py: Name "x" is not defined  [name-defined]']
    assert fixed == []


def test_fixing_one_error_does_not_license_another() -> None:
    baseline = fingerprints(_OUTPUT)
    current = Counter(baseline)
    del current['app/a.py: Returning Any from function declared to return "int"  [no-any-return]']
    current["app/a.py: Incompatible return value  [return-value]"] += 1
    new, fixed = compare(current, baseline)
    assert new == ["app/a.py: Incompatible return value  [return-value]"]
    assert len(fixed) == 1


def test_a_second_copy_of_a_recorded_error_is_new() -> None:
    baseline = fingerprints(_OUTPUT)
    current = baseline + fingerprints(_OUTPUT.splitlines()[0])
    new, _ = compare(current, baseline)
    assert len(new) == 1
