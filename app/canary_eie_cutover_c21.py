"""CANARY (DO NOT MERGE): EIE org-CI cutover H3, leaf witness for local_pr_pipeline/check_compliance_terminology.py (C-21).

Production code under app/ importing the deprecated chassis router must fail `make validate`.
"""

import chassis.router  # noqa: F401
