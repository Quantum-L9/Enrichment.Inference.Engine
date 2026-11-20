#!/usr/bin/env bash
# Odoo `converge` e2e: client -> Gate -> EIE -> Gate -> client, on
# Constellation.Gate's Docker rail narrowed by compose.converge.yml.
#
#   bash tests/e2e/run_converge_e2e.sh
#
# Needs: docker, images l9e2e/gate:local and l9e2e/eie:local (README.md), and a
# Constellation.Gate checkout — L9_E2E_GATE_RAIL, or side by side under
# L9_E2E_WORKSPACE (default /home/user). Fails closed when the rail is absent.
# Every run starts from zero (down -v) and tears down unless L9_E2E_KEEP_UP=1.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
GATE_RAIL="${L9_E2E_GATE_RAIL:-${L9_E2E_WORKSPACE:-/home/user}/Constellation.Gate/constellation-gate/tests/e2e/docker}"
STATE_DIR="${L9_E2E_STATE_DIR:-/root/l9e2e}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
EV="$STATE_DIR/evidence-converge/$(date -u +%Y%m%dT%H%M%SZ)"
BASE_ENV="$STATE_DIR/run.env"
ENV_FILE="$STATE_DIR/converge.env"

[[ -f "$GATE_RAIL/compose.yml" ]] || { echo "FATAL: no Gate rail at $GATE_RAIL" >&2; exit 1; }
for image in l9e2e/gate:local l9e2e/eie:local; do
  docker image inspect "$image" >/dev/null || { echo "FATAL: image $image not built" >&2; exit 1; }
done
mkdir -p "$EV"
echo "evidence: $EV"

# Credentials are ephemeral, live outside the repository and are never copied
# into the evidence directory. The rail generates its own; this adds the one
# consumer identity (odoo-e2e) and Gate's keyring for this proof: EIE + consumer.
bash "$GATE_RAIL/scripts/gen_env.sh" "$BASE_ENV" >/dev/null
python3 - "$BASE_ENV" "$ENV_FILE" <<'PY'
import json, os, secrets, sys

base = dict(line.strip().partition("=")[::2] for line in open(sys.argv[1]) if "=" in line)
odoo_key = secrets.token_hex(32)
ring = {"eie-e2e": base["L9E2E_EIE_KEY"], "odoo-e2e": odoo_key}
os.umask(0o077)
with open(sys.argv[2], "w") as fh:
    fh.write(open(sys.argv[1]).read())
    fh.write(f"L9E2E_ODOO_KEY={odoo_key}\nL9E2E_GATE_VERIFYING_KEYS_JSON={json.dumps(ring)}\n")
PY

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$GATE_RAIL/compose.yml" -f "$HERE/compose.converge.yml")
teardown() {
  for c in gate eie eie-pg; do docker logs "l9e2e-$c" > "$EV/$c.log" 2>&1 || true; done
  [[ "${L9_E2E_KEEP_UP:-0}" == "1" ]] || "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap teardown EXIT

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --wait --wait-timeout 300
docker ps -a --format '{{.Names}} {{.Status}}' | grep l9e2e | tee "$EV/boot_state.txt"

# Postgres fixture: EIE's entrypoint already ran `alembic upgrade head` against
# it. Prove the schema is at head from EIE's own migrations; a failure stops here.
docker exec -w /app l9e2e-eie alembic current 2>&1 | tee "$EV/eie_migrations.txt"
grep -q "(head)" "$EV/eie_migrations.txt" || { echo "FATAL: EIE schema is not at head" >&2; exit 1; }

# EIE registers with Gate during startup, before it reports healthy; if that
# failed, Gate has no owner for `converge` and the first assertion fails.
cd "$ROOT"
EIE_E2E=1 EIE_E2E_ENV_FILE="$ENV_FILE" NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost" \
  PYTHONPATH=. "$PYTHON" -m pytest tests/e2e -p no:xdist -p no:cacheprovider --timeout=300 --tb=short -rA \
  --junitxml="$EV/junit.xml" | tee "$EV/pytest.txt"
