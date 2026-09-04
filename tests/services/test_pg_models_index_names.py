"""Every index name in the persistence metadata is unique.

`ConvergenceRun.state` declared both `index=True` and an explicit
`Index("ix_convergence_runs_state")`; SQLAlchemy emitted two CREATE INDEX
statements with the same name and `create_all()` failed on a fresh database.
"""

from __future__ import annotations

from collections import Counter

from app.services.pg_models import Base, ConvergenceRun


def test_index_names_are_unique_across_metadata() -> None:
    names = [idx.name for table in Base.metadata.sorted_tables for idx in table.indexes]
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    assert not duplicates, f"duplicate index names: {duplicates}"


def test_convergence_runs_state_has_exactly_one_index() -> None:
    state_indexes = [
        idx
        for idx in ConvergenceRun.__table__.indexes
        if [c.name for c in idx.columns] == ["state"]
    ]
    assert len(state_indexes) == 1
    assert state_indexes[0].name == "ix_convergence_runs_state"
