"""
What a capture's record says about the page's server: its address and the
hosts it went through, each looked up with the enrichers (enrichers/), kept
under the record's ``enrichment``. A lookup that fails or runs out of time is
recorded as such; it never fails the capture.
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .record import now_iso


def hosts_of(client: Any) -> List[Tuple[str, str]]:
    """What to look up for a capture: (kind, value), in order, without repeats."""
    found: List[Tuple[str, str]] = []
    seen = set()

    def add(kind: str, value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in seen:
            seen.add(value.strip())
            found.append((kind, value.strip()))

    def host_of(url: Any) -> Optional[str]:
        if not isinstance(url, str):
            return None
        try:
            return urlsplit(url).hostname
        except ValueError:
            return None

    if not isinstance(client, dict):
        return found
    network = client.get("network") if isinstance(client.get("network"), dict) else {}
    main = network.get("mainFrame") if isinstance(network.get("mainFrame"), dict) else {}
    page = client.get("page") if isinstance(client.get("page"), dict) else {}
    add("ip", main.get("ip"))
    add("host", host_of(page.get("documentUrl")))
    for hop in main.get("redirects") or []:
        if isinstance(hop, dict):
            add("host", host_of(hop.get("from")))
            add("host", host_of(hop.get("to")))
    add("host", host_of(page.get("tabUrl")))
    return found


async def enrichment(service: Any, client: Any, *, enabled: bool, timeout: float) -> Dict[str, Any]:
    """Look the page's address and hosts up, within ``timeout`` seconds in all; never fails: each item is a
    result or an error. ``service`` is the EnrichService, or None while enrichers are off."""
    if not enabled:
        return {"skipped": True}
    started = now_iso()
    wanted = hosts_of(client)
    if service is None or not wanted:
        return {"startedAt": started, "finishedAt": now_iso(), "items": []}

    async def one(kind: str, value: str) -> Dict[str, Any]:
        result = await service.enrich(value)
        return {"value": value, "type": result.get("type", kind), "results": result}

    tasks = [asyncio.ensure_future(one(kind, value)) for kind, value in wanted]
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    items = []
    for (kind, value), task in zip(wanted, tasks):
        if task in pending:
            items.append({"value": value, "type": kind, "error": "timeout"})
        elif task.exception() is not None:
            error = task.exception()
            items.append({"value": value, "type": kind, "error": str(error)[:500] or type(error).__name__})
        else:
            items.append(task.result())
    return {"startedAt": started, "finishedAt": now_iso(), "items": items}
