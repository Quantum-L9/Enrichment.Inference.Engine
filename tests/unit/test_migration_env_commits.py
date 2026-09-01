"""`alembic upgrade head` must actually commit.

Regression guard for a silent failure: `run_async_migrations` configured the
context in one `run_sync` call and ran the migrations in a second callable that
discarded its connection, with no `context.begin_transaction()` anywhere. Alembic
logged "Running upgrade  -> 001" and returned 0, and the DDL was rolled back when
the connection closed — `alembic upgrade head` reported success against a
database with zero tables.

Static because it must hold on every runner, including those with no PostgreSQL.
The behavioural proof against a real database lives in the real-Postgres suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

ENV_PY = Path(__file__).resolve().parents[2] / "migrations" / "env.py"


def _func(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"migrations/env.py must define {name}()")


def _calls(node: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                names.add(fn.attr)
            elif isinstance(fn, ast.Name):
                names.add(fn.id)
    return names


def test_online_migrations_run_inside_begin_transaction():
    """The `with context.begin_transaction()` block is what commits the DDL."""
    fn = _func("do_run_migrations")
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
    assert withs, "do_run_migrations must open a transaction context"

    guarded = any(
        "begin_transaction" in _calls(item.context_expr)
        and any("run_migrations" in _calls(stmt) for stmt in w.body)
        for w in withs
        for item in w.items
    )
    assert guarded, (
        "context.run_migrations() must run inside `with context.begin_transaction():` "
        "— without it the DDL is rolled back and `alembic upgrade head` silently no-ops"
    )


def test_offline_migrations_run_inside_begin_transaction():
    fn = _func("run_migrations_offline")
    assert "begin_transaction" in _calls(fn)
    assert "run_migrations" in _calls(fn)


def test_configure_and_run_share_one_sync_callable():
    """Both halves need the same Connection; splitting them drops it."""
    fn = _func("do_run_migrations")
    calls = _calls(fn)
    assert {"configure", "run_migrations"} <= calls, (
        "context.configure() and context.run_migrations() must both live in the "
        f"single run_sync callable, got {sorted(calls)}"
    )

    runner = _func("run_async_migrations")
    run_sync_calls = [
        n
        for n in ast.walk(runner)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run_sync"
    ]
    assert len(run_sync_calls) == 1, (
        "run_async_migrations must make exactly one run_sync call passing "
        f"do_run_migrations; found {len(run_sync_calls)}"
    )


def test_no_lambda_discards_the_migration_connection():
    """`lambda _: context.run_migrations()` is the exact shape that broke."""
    runner = _func("run_async_migrations")
    for node in ast.walk(runner):
        if isinstance(node, ast.Lambda) and "run_migrations" in _calls(node):
            raise AssertionError(
                "run_migrations() must not be called from a lambda that discards "
                "the sync Connection handed to it by run_sync()"
            )
