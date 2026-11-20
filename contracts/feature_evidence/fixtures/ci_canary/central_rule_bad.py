"""Known-bad witness for the central subprocess shell rule. Scan input only."""

import subprocess


def run_listing(directory: str) -> str:
    command = f"ls {directory}"
    completed = subprocess.run(command, shell=True, capture_output=True, check=False)
    return completed.stdout.decode()
