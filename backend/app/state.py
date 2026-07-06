from __future__ import annotations

from .config import settings


class Runtime:
    """Mutable runtime state. The active namespace can be changed at runtime via the
    API (namespace dropdown) without restarting; it defaults to the configured one."""

    def __init__(self) -> None:
        self.namespace: str = settings.k8s_namespace


runtime = Runtime()
