"""Framework-agnostic compatibility export for telemetry setup.

Concrete FastAPI instrumentation is owned by :mod:`app.bootstrap.telemetry`,
the bootstrap composition boundary. Existing callers may continue to import
``setup_telemetry`` from this module while they migrate to the bootstrap path.
"""

from app.bootstrap.telemetry import setup_telemetry

__all__ = ["setup_telemetry"]
