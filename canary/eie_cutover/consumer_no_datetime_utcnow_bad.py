"""Canary: l9.eie.no-datetime-utcnow."""

from datetime import datetime


def stamp() -> datetime:
    return datetime.utcnow()
