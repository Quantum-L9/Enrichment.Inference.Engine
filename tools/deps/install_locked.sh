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
