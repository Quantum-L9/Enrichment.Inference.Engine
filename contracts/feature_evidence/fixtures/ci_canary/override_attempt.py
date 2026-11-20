"""Consumer override attempt: an inline suppression of a central rule.

A consumer must not be able to remove a centrally required finding. If the
central scan reports no finding on the call below, the suppression reduced
coverage. Scan input only.
"""

import subprocess


def run_listing(directory: str) -> str:
    command = f"ls {directory}"
    # nosemgrep
    completed = subprocess.run(command, shell=True, capture_output=True, check=False)
    return completed.stdout.decode()
