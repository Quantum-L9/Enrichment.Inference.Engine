"""Canary: l9.eie.no-pickle-loads."""

import pickle


def load(blob: bytes) -> object:
    return pickle.loads(blob)
