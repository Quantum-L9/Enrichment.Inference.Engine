"""Canary: l9.eie.singleton-requires-lock."""

_INSTANCE = None


def get_instance() -> object:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = object()
    return _INSTANCE
