"""loki_probe: non-breaking line probes (capture local variables at file:line in a running service)."""
from .core import MAX_SECONDS, ProbeError, ProbeManager, manager

__all__ = ["MAX_SECONDS", "ProbeError", "ProbeManager", "manager"]
