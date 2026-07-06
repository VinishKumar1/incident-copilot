from __future__ import annotations

import asyncio
import logging
import time

from .config import settings
from .source import fetch_recent_errors
from .store import store

log = logging.getLogger("poller")


async def poll_loop() -> None:
    """Background task: pull recent errors from Loki on an interval and group them."""
    log.info("poller started (mock=%s, interval=%ss)", settings.use_mock, settings.poll_interval_seconds)
    while True:
        try:
            entries = await fetch_recent_errors()
            if entries:
                await store.ingest(entries)
            store.last_poll_ts = time.time()
            store.last_error = None
        except asyncio.CancelledError:
            log.info("poller stopping")
            raise
        except Exception as exc:  # keep the loop alive on transient failures
            store.last_error = str(exc)
            log.warning("poll failed: %s", exc)
            # Recover from a wedged connection by forcing a reconnect next cycle.
            if settings.log_source == "k8s":
                try:
                    from .k8s import k8s_client

                    k8s_client.reset()
                except Exception:
                    pass
        await asyncio.sleep(settings.poll_interval_seconds)
