"""Known-good twin of local_rule_bad.py. Scan input only."""

from datetime import UTC, datetime


def stamp() -> str:
    return datetime.now(UTC).isoformat()
