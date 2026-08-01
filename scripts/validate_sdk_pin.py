#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = "a770e8531dc1c59ce01e1dbb0f4162785d9dda89"
errors = []
for rel in ["pyproject.toml", "requirements-ci.txt"]:
    text = (ROOT / rel).read_text()
    if PIN not in text:
        errors.append(f"{rel}: missing pin")
    if "Gate_SDK.git@main" in text:
        errors.append(f"{rel}: floats on main")
if errors:
    print("FAIL")
    print("\n".join(errors))
    raise SystemExit(1)
print("PASS EIE pin", PIN)
