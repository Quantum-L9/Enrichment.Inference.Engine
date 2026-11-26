# RETIRED (EIE-006). Not the live Odoo producer, and not installed by IB-Odoo_19:
# the live consumer is IB-Odoo_19 plasticos_enrichment via plasticos_gate, which
# reaches EIE through the Gate action `converge`
# (docs/contracts/dependencies/odoo-crm.yaml). Its `data` files were never
# shipped and `plasticos_inference_engine` is retired upstream, so the module
# cannot install. Kept as reference source only; removal is a separate cleanup.
{  # noqa: B018 — Odoo manifest convention
    "name": "Plasticos Research Enrichment v2",
    "version": "19.0.2.0.0",
    "category": "Automation",
    "depends": [
        "base",
        "contacts",
        "plasticos_enrichment",
        "plasticos_inference_engine",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/menu.xml",
        "views/enrichment_profile_views.xml",
        "views/enrichment_run_views.xml",
        "data/cron.xml",
    ],
    "installable": False,
    "application": False,
}
