#!/usr/bin/env python3
"""Generate ephemeral ed25519 identities for one gate_loop run (TEST_ONLY).

Never deployment material, never committed: the env file is written OUTSIDE
the repository with mode 0600, and only key ids and public-key fingerprints
are ever recorded as evidence.

Gate's own PUBLIC key is in Gate's verifying map on purpose (deviation D-2):
Gate at b451b68 resolves its own key id to its private signing key
(config/settings.py resolve_verifying_key), which only works for HMAC; under
ed25519 its dispatch packet then fails its own outbound validation (F-A7-01).

Identities: gate-gl (Gate), eie-gl (enrichment-engine worker), ceg-gl (graph
worker), odoo-gl (consumer), op-gl (operator consumer), rogue-gl (a well-formed
key Gate does NOT trust).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

IDS = ("gate-gl", "eie-gl", "ceg-gl", "odoo-gl", "op-gl", "rogue-gl")
TRUSTED_BY_GATE = ("gate-gl", "eie-gl", "ceg-gl", "odoo-gl", "op-gl")


def main(out: str) -> None:
    priv, pub, fp = {}, {}, {}
    for kid in IDS:
        k = Ed25519PrivateKey.generate()
        priv[kid] = base64.b64encode(k.private_bytes_raw()).decode()
        raw_pub = k.public_key().public_bytes_raw()
        pub[kid] = base64.b64encode(raw_pub).decode()
        fp[kid] = hashlib.sha256(raw_pub).hexdigest()[:16]
    gate_verifying = {k: pub[k] for k in TRUSTED_BY_GATE}
    worker_verifying = {"gate-gl": pub["gate-gl"]}
    # Diagnostic HMAC profile (class D, never PASS for a signed claim): one
    # distinct secret per identity, used only with GL_SIGNING=hmac.
    hm = {kid: base64.b64encode(secrets.token_bytes(32)).decode() for kid in IDS}
    env = {
        **{f"GL_HMAC_{k.split('-')[0].upper()}": hm[k] for k in IDS},
        "GL_HMAC_GATE_VERIFYING_KEYS_JSON": json.dumps(
            {k: hm[k] for k in TRUSTED_BY_GATE if k != "gate-gl"}
        ),
        "GL_HMAC_WORKER_VERIFYING_KEYS_JSON": json.dumps({"gate-gl": hm["gate-gl"]}),
        "GL_HMAC_ALL_JSON": json.dumps(hm),
        **{f"GL_PRIV_{k.split('-')[0].upper()}": priv[k] for k in IDS},
        "GL_GATE_VERIFYING_KEYS_JSON": json.dumps(gate_verifying),
        "GL_WORKER_VERIFYING_KEYS_JSON": json.dumps(worker_verifying),
        "GL_ALL_PUBLIC_KEYS_JSON": json.dumps(pub),
        "GL_ADMIN_TOKEN": secrets.token_urlsafe(24),
        "GL_NEO4J_PASSWORD": secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"),
        "GL_PG_PASSWORD": secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"),
    }
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(
        fd, 0o600
    )  # an existing file keeps its mode on open(); force 0600 before writing secrets
    with os.fdopen(fd, "w") as f:
        for k, v in env.items():
            f.write(f"{k}='{v}'\n")
    print(
        json.dumps(
            {
                "env_file": out,
                "algorithm": "ed25519",
                "key_ids": list(IDS),
                "public_key_fingerprints": fp,
                "secrets_printed": False,
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/root/l9e2e/gate_loop/run.env")
