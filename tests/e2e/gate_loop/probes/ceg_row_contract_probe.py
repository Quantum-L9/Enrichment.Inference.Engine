"""Run inside the CEG image: CEG's own SyncRowContract over the same vectors.

Observational only. argv: <ceg_pack_root> <vectors.json>
"""

import json
import sys

from engine.config.loader import DomainPackLoader
from engine.sync.row_contract import SyncRowContract

root, vec_path = sys.argv[1], sys.argv[2]
with open(vec_path) as fh:
    vectors = json.load(fh)
loader = DomainPackLoader(root)
out = []
for v in vectors["vectors"]:
    spec = loader.load_domain(v["tenant"])
    ep = next(e for e in spec.sync.endpoints if e.path.rsplit("/", 1)[-1] == v["entity_type"])
    contract = SyncRowContract.from_spec(spec, ep)
    for refuse in (False, True):
        defects = contract.defects([v["row"]], refuse_undeclared=refuse)
        out.append(
            {"id": v["id"], "refuse_undeclared": refuse, "defects": [str(d) for d in defects]}
        )
print(json.dumps(out, indent=1))
