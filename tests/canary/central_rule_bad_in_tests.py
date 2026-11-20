"""Scope witness: the central known-bad construct placed under tests/.

Records whether the central scan covers tests/. Not a test module: pytest
collects test_*.py only. Scan input only.
"""

import subprocess


def run_listing(directory: str) -> str:
    command = f"ls {directory}"
    completed = subprocess.run(command, shell=True, capture_output=True, check=False)
    return completed.stdout.decode()
