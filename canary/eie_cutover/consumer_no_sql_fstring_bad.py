"""Canary: l9.eie.no-sql-fstring."""


def query(table: str) -> str:
    return f"SELECT * FROM {table}"
