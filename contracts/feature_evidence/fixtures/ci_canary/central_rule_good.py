"""Known-good twin of central_rule_bad.py. Scan input only."""

import subprocess


def run_listing(directory: str) -> str:
    completed = subprocess.run(["ls", directory], shell=False, capture_output=True, check=False)
    return completed.stdout.decode()
