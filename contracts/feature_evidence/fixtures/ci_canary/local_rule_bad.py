"""Known-bad witness for EIE's local no-datetime-utcnow rule. Scan input only."""

from datetime import datetime


def stamp() -> str:
    return datetime.utcnow().isoformat()
