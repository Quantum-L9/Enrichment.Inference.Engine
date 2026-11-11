#!/usr/bin/env bash
# Install a dependency profile from uv.lock, the repository's single lock.
#
#   bash tools/deps/install_locked.sh full     # project (editable) + every group
#   bash tools/deps/install_locked.sh runtime  # [project] dependencies + licenses (Dockerfile.prod)
#   bash tools/deps/install_locked.sh uv       # uv only (for `uv lock --check`)
#
# `uv sync --locked` installs exactly the versions uv.lock records and verifies
# every distribution against the sha256 the lock holds; it fails when uv.lock
# no longer matches pyproject.toml instead of re-resolving. The Gate SDK is a
# git source pinned to the commit uv.lock records (scripts/validate_sdk_pin.py);
# every other package installs from a locked wheel
# (tests/unit/test_uv_lock.py). uv itself is installed first from
# tools/deps/uv-bootstrap.txt with pip --require-hashes.
#
# The project is a setuptools package: `full` installs it editable (what
# `pip install -e ".[dev]"` used to do). `runtime` installs its dependencies
# only: the production image runs the app from /app on PYTHONPATH and never
# pip-installs the tree, so its dependency layer needs nothing but
# pyproject.toml and uv.lock.
#
# Core-owned tools (pip, pre-commit, ruff, mypy, pytest) are not in uv.lock
# ([tool.uv] exclude-dependencies). The full profile installs them from
# Quantum-L9/l9-ci-core's installer pins at the v2 tag (L9_CI_CORE_REF), the
# same file install-consumer-ci@v2 installs, so a Core bump reaches this
# repository with no edit here. Every line must be an exact name==version pin.
#
# Target environment: UV_PROJECT_ENVIRONMENT when set (Docker: /opt/venv),
# otherwise the environment of $PYTHON (default: python on PATH), so a CI
# step's later `python -m pytest` sees what was installed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python}"

case "${1:-}" in
  full) args=(--all-groups) ;;
  runtime) args=(--no-default-groups --group licenses --no-install-project) ;;
  uv) args=() ;;
  *) echo "usage: $0 full|runtime|uv" >&2; exit 2 ;;
esac

# Keep --require-hashes on this command line. Scorecard's pin probe does not
# follow a bash array, so a split `pip install "${args[@]}"` is an unpinned pipCommand.
"$PYTHON" -m pip install --disable-pip-version-check --quiet --no-deps --require-hashes -r "$ROOT/tools/deps/uv-bootstrap.txt"
[[ "$1" == "uv" ]] && exit 0

if [[ -z "${UV_PROJECT_ENVIRONMENT:-}" ]]; then
  UV_PROJECT_ENVIRONMENT="$("$PYTHON" -c 'import sys; print(sys.prefix)')"
fi
export UV_PROJECT_ENVIRONMENT
export UV_PYTHON_DOWNLOADS=never

# --inexact keeps what the target already holds (pip, uv itself, a runner's
# own packages); --locked refuses a uv.lock that pyproject.toml has outgrown.
"$PYTHON" -m uv sync --project "$ROOT" --locked --inexact "${args[@]}"

case "$1" in
  full) ;;
  *) exit 0 ;;
esac

CORE_REF="${L9_CI_CORE_REF:-v2}"
CORE_PINS_URL="https://raw.githubusercontent.com/Quantum-L9/l9-ci-core/${CORE_REF}/.github/actions/install-consumer-ci/requirements-consumer-ci.txt"
pins="$(mktemp)"
trap 'rm -f "$pins"' EXIT
# Python, not curl: the slim Docker base images carry no curl.
"$PYTHON" - "$CORE_PINS_URL" "$pins" <<'PY'
import re
import sys
import urllib.request

url, dest = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
    text = response.read().decode("utf-8")
pins = []
for raw in text.splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if not re.fullmatch(r"[A-Za-z0-9._-]+==[0-9][^\s]*", line):
        sys.exit(f"install_locked: Core pin line is not name==version: {line!r}")
    pins.append(line)
if not pins:
    sys.exit(f"install_locked: no Core pins at {url}")
with open(dest, "w", encoding="utf-8") as stream:
    stream.write("\n".join(pins) + "\n")
print("install_locked: Core tools " + " ".join(pins))
PY

if [[ -x "$UV_PROJECT_ENVIRONMENT/bin/python" ]]; then
  target="$UV_PROJECT_ENVIRONMENT/bin/python"
else
  target="$PYTHON"
fi
# --only-binary :all: as Core's installer does: installing a tool never runs an sdist build.
# --no-config: `uv pip` otherwise reads [tool.uv] from pyproject.toml, whose
# exclude-dependencies drops exactly these tools and reports success.
"$PYTHON" -m uv pip install --no-config --python "$target" --only-binary :all: -r "$pins"
# Fail closed: prove each pin is what the target now imports.
"$target" - "$pins" <<'PY'
import sys
from importlib.metadata import PackageNotFoundError, version

wrong = []
for line in open(sys.argv[1], encoding="utf-8").read().split():
    name, pinned = line.split("==", 1)
    try:
        found = version(name)
    except PackageNotFoundError:
        found = "missing"
    if found != pinned:
        wrong.append(f"{name}: want {pinned}, have {found}")
if wrong:
    sys.exit("install_locked: Core tools not installed: " + "; ".join(wrong))
PY
