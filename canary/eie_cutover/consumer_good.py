"""Canary known-good: the compliant form of each consumer rule; expects no consumer finding."""

import threading
from datetime import UTC, datetime

import httpx

_LOCK = threading.Lock()
_INSTANCE = None


def stamp() -> datetime:
    return datetime.now(UTC)


async def fetch(url: str) -> int:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    return response.status_code


def get_instance() -> object:
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = object()
    return _INSTANCE


def parse(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None
