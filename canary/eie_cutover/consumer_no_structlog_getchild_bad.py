"""Canary: l9.eie.no-structlog-getchild."""

import structlog

logger = structlog.get_logger(__name__)
child = logger.getChild("canary")
