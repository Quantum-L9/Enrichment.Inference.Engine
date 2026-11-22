"""Run inside the EIE image: EIE's own compatibility contract and row projection.

Observational only. argv: <eie_cartridge_root> <ceg_pack_root> <vectors.json>
Prints one JSON document: compatibility verdict per tenant + projection outcome per vector.
"""
import json, sys
from pathlib import Path
import yaml
from app.engines.domain_spec_compatibility import check_compatibility
from app.engines.packet_router import resolve_graph_sync_contract, project_graph_sync_row

eie_root, ceg_root, vec_path = (Path(a) for a in sys.argv[1:4])
vectors = json.loads(vec_path.read_text())
out = {"compatibility": {}, "projection": []}
specs = {}
for tenant in sorted({v["tenant"] for v in vectors["vectors"]}):
    eie = yaml.safe_load((eie_root / tenant / "spec.yaml").read_text())
    ceg = yaml.safe_load((ceg_root / tenant / "spec.yaml").read_text())
    specs[tenant] = eie
    out["compatibility"][tenant] = check_compatibility(eie, ceg)
for v in vectors["vectors"]:
    contract = resolve_graph_sync_contract(specs[v["tenant"]])
    idp = contract.id_property
    fields = {k: val for k, val in v["row"].items() if k != idp}
    try:
        proj = project_graph_sync_row(entity_id=v["row"][idp], fields=fields, contract=contract)
        d = {k: getattr(proj, k) for k in ("row", "defects", "dropped") if hasattr(proj, k)} or repr(proj)
        out["projection"].append({"id": v["id"], "entity_type": contract.entity_type, "result": d})
    except Exception as exc:  # observed refusal is a result, not an error
        out["projection"].append({"id": v["id"], "refused": f"{type(exc).__name__}: {exc}"})
print(json.dumps(out, indent=1, default=str))
