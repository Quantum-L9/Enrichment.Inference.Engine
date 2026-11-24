#!/usr/bin/env python3
"""Generate ephemeral ed25519 identities for one gate_loop run (TEST_ONLY).

Never deployment material, never committed: the env file is written OUTSIDE
the repository with mode 0600, and only key ids and public-key fingerprints
are ever recorded as evidence.

Gate's verifying map holds only the identities that call Gate: Gate resolves
its own key id from its private key (Constellation.Gate 2384923, D-4), so the
former deviation that put Gate's own public key there is gone. GL_KEYSET marks
this layout; rail.sh regenerates an env file written by an older layout.

Every verifying key is declared with its algorithm (L9_VERIFYING_KEY_ALGORITHMS_JSON,
Gate_SDK 1.3.4): a raw 32-byte key is either an ed25519 public key or an HMAC
secret, and 1.3.4 refuses such an undeclared key when signatures are required.

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
import tempfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

IDS = ("gate-gl", "eie-gl", "ceg-gl", "odoo-gl", "op-gl", "rogue-gl")
TRUSTED_BY_GATE = ("eie-gl", "ceg-gl", "odoo-gl", "op-gl")
KEYSET = "3"


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
    neo4j_secret = secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")
    pg_secret = secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")
    env = {
        "GL_KEYSET": KEYSET,
        **{f"GL_HMAC_{k.split('-')[0].upper()}": hm[k] for k in IDS},
        "GL_HMAC_GATE_VERIFYING_KEYS_JSON": json.dumps({k: hm[k] for k in TRUSTED_BY_GATE}),
        "GL_HMAC_WORKER_VERIFYING_KEYS_JSON": json.dumps({"gate-gl": hm["gate-gl"]}),
        "GL_HMAC_ALL_JSON": json.dumps(hm),
        **{f"GL_PRIV_{k.split('-')[0].upper()}": priv[k] for k in IDS},
        "GL_GATE_VERIFYING_KEYS_JSON": json.dumps(gate_verifying),
        "GL_WORKER_VERIFYING_KEYS_JSON": json.dumps(worker_verifying),
        "GL_GATE_VERIFYING_KEY_ALGORITHMS_JSON": json.dumps(
            dict.fromkeys(gate_verifying, "ed25519")
        ),
        "GL_WORKER_VERIFYING_KEY_ALGORITHMS_JSON": json.dumps({"gate-gl": "ed25519"}),
        "GL_HMAC_GATE_VERIFYING_KEY_ALGORITHMS_JSON": json.dumps(
            dict.fromkeys(TRUSTED_BY_GATE, "hmac-sha256")
        ),
        "GL_HMAC_WORKER_VERIFYING_KEY_ALGORITHMS_JSON": json.dumps({"gate-gl": "hmac-sha256"}),
        "GL_ALL_PUBLIC_KEYS_JSON": json.dumps(pub),
        "GL_ADMIN_TOKEN": secrets.token_urlsafe(24),
        "GL_NEO4J_SECRET": neo4j_secret,
        "GL_PG_SECRET": pg_secret,
        # The Gate rail's compose.yml interpolates these names (${L9E2E_*:?}); the
        # values are generated here and live only in run.env, outside the repository.
        "L9E2E_NEO4J_PASSWORD": neo4j_secret,
        "L9E2E_PG_PASSWORD": pg_secret,
    }
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    # Write a private temp file beside the target and publish it only once it is complete and
    # fsynced: a failed run leaves any previous file untouched and no partial file behind
    # (rail.sh regenerates only when run.env is absent).
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out) or ".", prefix=".run.env.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in env.items():
                f.write(f"{k}='{v}'\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    sys.stdout.write(
        json.dumps(
            {
                "env_file": out,
                "algorithm": "ed25519",
                "key_ids": list(IDS),
                "public_key_fingerprints": fp,
                "secrets_printed": False,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/root/l9e2e/gate_loop/run.env")
