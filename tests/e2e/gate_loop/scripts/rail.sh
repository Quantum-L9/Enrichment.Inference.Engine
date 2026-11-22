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
ENV_FILE="${GL_ENV_FILE:-$STATE/run.env}"
: "${GL_GATE_RAIL:?path to Constellation.Gate constellation-gate/tests/e2e/docker}"
: "${GL_CEG_REPO:?path to the Cognitive.Engine.Graphs checkout}"

mkdir -p "$STATE"
[[ -f "$ENV_FILE" ]] || python3 "$HERE/gen_keys.py" "$ENV_FILE" >"$STATE/keys.receipt.json"

# The base rail interpolates L9E2E_* with ${VAR:?}. This overlay replaces every
# one of those values, so the aliases below only satisfy interpolation; the
# overridden services never see them.
compose_env() {
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
  export L9E2E_GATE_KEY="$GL_PRIV_GATE" L9E2E_EIE_KEY="$GL_PRIV_EIE" L9E2E_CEG_KEY="$GL_PRIV_CEG"
  export L9E2E_ADMIN_TOKEN="$GL_ADMIN_TOKEN" L9E2E_NEO4J_PASSWORD="$GL_NEO4J_PASSWORD"
  export L9E2E_PG_PASSWORD="$GL_PG_PASSWORD" L9E2E_VERIFYING_KEYS_JSON="$GL_GATE_VERIFYING_KEYS_JSON"
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
      docker exec l9e2e-neo4j cypher-shell -u neo4j -p "$GL_NEO4J_PASSWORD" -d system \
        "CREATE DATABASE $db IF NOT EXISTS WAIT;" >/dev/null 2>&1 && break
      sleep 5
    done
  done
}

case "${1:-status}" in
  up)
    compose_env; assemble_domains
    COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
    COMPOSE up -d --wait --wait-timeout 300 || true
    neo4j_dbs
    docker restart l9e2e-ceg >/dev/null
    for _ in $(seq 1 60); do
      n="$(curl -sS --noproxy '*' -H "X-Admin-Token: $GL_ADMIN_TOKEN" http://127.0.0.1:19000/v1/registry 2>/dev/null \
           | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)))
except Exception: print(0)')"
      [[ "$n" == "2" ]] && break
      sleep 5
    done
    "$0" status ;;
  down) compose_env; COMPOSE down -v --remove-orphans ;;
  status)
    compose_env
    COMPOSE ps --format '{{.Name}} {{.State}} {{.Health}}'
    echo "registry:"; curl -sS --noproxy '*' -H "X-Admin-Token: $GL_ADMIN_TOKEN" http://127.0.0.1:19000/v1/registry | python3 -c 'import json,sys
d=json.load(sys.stdin); print(json.dumps({k: sorted(v.get("supported_actions", [])) for k, v in d.items()}) if isinstance(d, dict) else d)' || true
    echo "ready:"; curl -sS --noproxy '*' http://127.0.0.1:19000/v1/ready; echo ;;
  compose) shift; compose_env; COMPOSE "$@" ;;
  *) echo "usage: $0 up|down|status|compose <args>" >&2; exit 2 ;;
esac
