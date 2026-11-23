#!/usr/bin/env bash
# gate_loop rail lifecycle (TEST_ONLY): up | down | status | env
#
#   GL_GATE_RAIL=<Constellation.Gate>/constellation-gate/tests/e2e/docker \
#   GL_CEG_REPO=<Cognitive.Engine.Graphs checkout> \
#   bash tests/e2e/gate_loop/scripts/rail.sh up
#
# Images are l9e2e/{gate,eie,ceg}:local from the Gate rail's build_images.sh,
# each labelled with the commit it was built from. Nothing here builds or
# patches an image, and nothing writes into the Gate or CEG checkouts.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GL_DIR="$(dirname "$HERE")"
EIE_ROOT="$(cd "$GL_DIR/../../.." && pwd)"
STATE="${GL_STATE_DIR:-/root/l9e2e/gate_loop}"
: "${GL_GATE_RAIL:?path to Constellation.Gate constellation-gate/tests/e2e/docker}"
: "${GL_CEG_REPO:?path to the Cognitive.Engine.Graphs checkout}"
# Compose resolves a relative bind source against the first compose file's directory (the
# Gate checkout), not the caller's: export only absolute paths for the fixture mounts.
mkdir -p "$STATE"
STATE="$(cd "$STATE" && pwd)"
GL_CEG_REPO="$(cd "$GL_CEG_REPO" && pwd)"
ENV_FILE="${GL_ENV_FILE:-$STATE/run.env}"

mkdir -p "$STATE"
# Regenerate when absent or written by an older gen_keys.py (no GL_NEO4J_SECRET).
if [[ ! -f "$ENV_FILE" ]] || ! grep -q '^GL_NEO4J_SECRET=' "$ENV_FILE"; then
  python3 "$HERE/gen_keys.py" "$ENV_FILE" >"$STATE/keys.receipt.json"
fi

# The base rail interpolates L9E2E_* with ${VAR:?}. This overlay replaces every
# one of those values, so the aliases below only satisfy interpolation; the
# overridden services never see them.
compose_env() {
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
  export L9E2E_GATE_KEY="$GL_PRIV_GATE" L9E2E_EIE_KEY="$GL_PRIV_EIE" L9E2E_CEG_KEY="$GL_PRIV_CEG"
  # The base rail's database credentials (L9E2E_*) come straight from run.env.
  export L9E2E_ADMIN_TOKEN="$GL_ADMIN_TOKEN" L9E2E_VERIFYING_KEYS_JSON="$GL_GATE_VERIFYING_KEYS_JSON"
  # P12 rig: GL_ROGUE_CEG=1 makes the graph worker sign its responses with the
  # well-formed but untrusted rogue-gl key.
  if [[ "${GL_ROGUE_CEG:-0}" == "1" ]]; then export GL_PRIV_CEG="$GL_PRIV_ROGUE" GL_CEG_KEY_ID=rogue-gl; fi
  export GL_EIE_DOMAINS="$STATE/eie_domains" GL_CEG_PACKS="$GL_CEG_REPO/tests/fixtures/test_only_packs"
}

# GL_SIGNING=ed25519 (default, the target) | hmac (diagnostic class D only).
GL_SIGNING="${GL_SIGNING:-ed25519}"
COMPOSE() {
  local extra=()
  [[ "$GL_SIGNING" == "hmac" ]] && extra=(-f "$GL_DIR/compose.gate_loop.hmac.yml")
  docker compose -p l9-constellation-e2e -f "$GL_GATE_RAIL/compose.yml" -f "$GL_DIR/compose.gate_loop.yml" "${extra[@]}" "$@"
}

assemble_domains() {
  rm -rf "$STATE/eie_domains"; mkdir -p "$STATE/eie_domains"
  cp -r "$EIE_ROOT/domains/plasticos" "$STATE/eie_domains/plasticos"
  cp -r "$GL_DIR/cartridges/synthneutral" "$STATE/eie_domains/synthneutral"
}

neo4j_dbs() {
  for db in plasticos synthneutral; do
    for _ in $(seq 1 30); do
      docker exec l9e2e-neo4j cypher-shell -u neo4j -p "$GL_NEO4J_SECRET" -d system \
        "CREATE DATABASE $db IF NOT EXISTS WAIT;" >/dev/null 2>&1 && break
      sleep 5
    done
  done
}

# Gate's registry as JSON. The response is saved to a file and then parsed by a
# checked-in script; no fetched content is ever executed.
registry_json() {
  local out="$STATE/registry.json"
  curl -sS --noproxy '*' --max-time 10 -H "X-Admin-Token: $GL_ADMIN_TOKEN" -o "$out" \
    http://127.0.0.1:19000/v1/registry 2>/dev/null || printf '{}' >"$out"
  cat "$out"
}

case "${1:-status}" in
  up)
    compose_env; assemble_domains
    COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
    COMPOSE up -d --wait --wait-timeout 300 || true
    neo4j_dbs
    docker restart l9e2e-ceg >/dev/null
    n=0
    for _ in $(seq 1 60); do
      n="$(registry_json | python3 "$HERE/registry_view.py" count)"
      [[ "$n" == "2" ]] && break
      sleep 5
    done
    "$0" status || true
    # The boot is best effort until here (databases must exist before CEG can
    # serve); from here it fails unless both workers registered and Gate is ready.
    [[ "$n" == "2" ]] || { echo "FAIL: $n of 2 workers registered" >&2; exit 1; }
    ready=""
    for _ in $(seq 1 24); do
      ready="$(curl -sS --noproxy '*' --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:19000/v1/ready || true)"
      [[ "$ready" == "200" ]] && break
      sleep 5
    done
    [[ "$ready" == "200" ]] || { echo "FAIL: Gate /v1/ready answered ${ready:-nothing}" >&2; exit 1; }
    # Readiness may not depend on registrations, so count them again after it.
    n="$(registry_json | python3 "$HERE/registry_view.py" count)"
    [[ "$n" == "2" ]] || { echo "FAIL: $n of 2 workers registered after readiness" >&2; exit 1; } ;;
  down) compose_env; COMPOSE down -v --remove-orphans ;;
  status)
    compose_env
    COMPOSE ps --format '{{.Name}} {{.State}} {{.Health}}'
    echo "registry:"; registry_json | python3 "$HERE/registry_view.py" actions || true
    echo "ready:"; curl -sS --noproxy '*' --max-time 10 http://127.0.0.1:19000/v1/ready || true; echo ;;
  compose) shift; compose_env; COMPOSE "$@" ;;
  *) echo "usage: $0 up|down|status|compose <args>" >&2; exit 2 ;;
esac
