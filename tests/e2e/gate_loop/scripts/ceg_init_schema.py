"""Operator deployment step for the gate_loop rail: run CEG's own admin init_schema for one tenant.

TEST_ONLY. Executed inside the CEG container (``docker exec -i l9e2e-ceg python - <tenant>``), never
through Gate: admin is operator-only (OPERATOR_ONLY_ACTIONS). It wires CEG's handler dependencies the
way engine/boot.py does and calls engine.handlers.handle_admin with subaction init_schema, so every
constraint comes from CEG's code, not from the rail. Prints CEG's answer as one JSON line.
"""

import asyncio
import json
import sys

from engine.config.loader import DomainPackLoader
from engine.config.settings import settings
from engine.graph.driver import GraphDriver
from engine.handlers import handle_admin, init_dependencies


async def main(tenant: str) -> None:
    driver = GraphDriver(
        uri=settings.neo4j_uri, username=settings.neo4j_username, password=settings.neo4j_password
    )
    await driver.connect()
    try:
        init_dependencies(driver, DomainPackLoader(config_path=str(settings.domains_root)))
        answer = await handle_admin(tenant, {"subaction": "init_schema", "domain_id": tenant})
        sys.stdout.write(json.dumps(answer) + "\n")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
