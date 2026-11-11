"""The two guarantees the retired baseline ratchet gave, kept by EIE itself (#253).

EIE's baseline-ratchet caller was deleted in #289 because the Core workflow it
called had been retired. That caller was the only enforcement behind the two
ledgers under ``.l9/baselines/``:

* ``packet-envelope.yml``: "any future PacketEnvelope reference fails CI".
  TransportPacket is the only wire model; the deprecated envelope class must not
  come back. The ledger is retired and this file is the check.
* ``test-quarantine.yml``: shrink-only quarantine of failing tests. The ledger
  is empty, and ``tools/pytest_quarantine_deselects.py`` still turns any entry
  into a ``--deselect`` in CI, so shrink-only from zero means it stays empty.

Neither guarantee is provided by Core's required ``Analyze (central Core)``
check: Core runs ``baseline scan-packet-envelope`` only in its own SDK contract
workflow, never against a consumer.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_LEDGER = ROOT / ".l9" / "baselines" / "test-quarantine.yml"

# Built from parts so this file is not itself a finding.
_RETIRED_ENVELOPE = re.compile(r"\b" + "Packet" + "Envelope" + r"\b")


def _tracked_python_files() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py", "*.pyi"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode()
        files = [ROOT / name for name in listed.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        # No git (an sdist or a container copy): walk the tree instead.
        skip = {".git", ".venv", "venv", "node_modules", "build", "dist"}
        files = [
            path
            for pattern in ("*.py", "*.pyi")
            for path in ROOT.rglob(pattern)
            if not skip.intersection(path.relative_to(ROOT).parts)
        ]
    assert files, "found no Python sources to scan"
    return files


def test_no_python_source_names_the_retired_envelope() -> None:
    findings = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for path in _tracked_python_files()
        if path.is_file()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _RETIRED_ENVELOPE.search(line)
    ]
    assert not findings, (
        "the deprecated packet envelope is retired; use the Gate SDK TransportPacket:\n"
        + "\n".join(findings)
    )


def test_scanner_finds_the_identifier() -> None:
    """The check is not vacuous: it matches the identifier and not prose."""
    assert _RETIRED_ENVELOPE.search("from chassis import " + "Packet" + "Envelope")
    assert not _RETIRED_ENVELOPE.search("# Packet Envelope helper")
    assert not _RETIRED_ENVELOPE.search("agents/protocols/packet-envelope.yaml")


def test_quarantine_ledger_stays_empty() -> None:
    data = yaml.safe_load(QUARANTINE_LEDGER.read_text(encoding="utf-8")) or {}
    assert data.get("entries") == [], (
        "a failing test is fixed, not quarantined: the ledger was burned down to "
        "zero (#139) and the retired ratchet only ever let it shrink"
    )
