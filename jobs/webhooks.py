"""
Webhooks: a POST to each configured URL when a job or an eval run finishes.

The body is ``{"event", "time", "data"}`` as JSON; ``X-Toolkit-Event`` names
the event. Each delivery carries the Standard Webhooks headers
(https://www.standardwebhooks.com): ``webhook-id``, the same across the
retries of one message, so a receiver can drop a duplicate;
``webhook-timestamp``, when it was first sent; and, with ``WEBHOOK_SECRET``
set, ``webhook-signature`` as ``v1,<base64 HMAC-SHA256 of "id.timestamp.body">``,
which any Standard Webhooks library verifies given the secret as
``whsec_<base64 of WEBHOOK_SECRET>``. ``X-Toolkit-Signature``
(``sha256=<hex HMAC-SHA256 of the body>``) is still sent for receivers that
check it. Delivery is in the background with a few retries; a URL that never
answers is logged and forgotten, never retried across restarts. The URLs are
``WEBHOOK_URLS`` plus the one a job, schedule or run names itself.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

EVENTS = ("job.finished", "run.finished", "test")
RETRY_AFTER_S = (1.0, 5.0, 20.0)


class Webhooks:
    def __init__(self, urls: Iterable[str] = (), secret: Optional[str] = None, timeout: float = 10.0):
        self.urls: List[str] = [u.strip() for u in urls if u and u.strip()]
        self.secret = secret or None
        self.timeout = timeout
        self._tasks: Set[asyncio.Task] = set()
        self._client: Optional[Any] = None  # httpx.AsyncClient, made on first use

    def describe(self) -> Dict[str, Any]:
        return {"urls": list(self.urls), "secret_set": self.secret is not None, "events": list(EVENTS)}

    def body(self, event: str, data: Any) -> bytes:
        return json.dumps({"event": event, "time": time.time(), "data": data}, default=str).encode()

    def headers(self, event: str, body: bytes, message_id: Optional[str] = None, sent_at: Optional[int] = None) -> Dict[str, str]:
        """The headers of one message: made once per message, so its retries carry the same id, time and signature."""
        message_id = message_id or f"msg_{uuid.uuid4().hex}"
        timestamp = str(sent_at if sent_at is not None else int(time.time()))
        headers = {
            "Content-Type": "application/json", "X-Toolkit-Event": event, "User-Agent": "ai-toolkit-webhook",
            "webhook-id": message_id, "webhook-timestamp": timestamp,
        }
        if self.secret:
            key = self.secret.encode()
            signed = hmac.new(key, b".".join((message_id.encode(), timestamp.encode(), body)), hashlib.sha256).digest()
            headers["webhook-signature"] = "v1," + base64.b64encode(signed).decode()
            headers["X-Toolkit-Signature"] = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
        return headers

    @property
    def standard_secret(self) -> Optional[str]:
        """WEBHOOK_SECRET as a Standard Webhooks library wants it (``whsec_`` and base64); None without one."""
        return "whsec_" + base64.b64encode(self.secret.encode()).decode() if self.secret else None

    def send(self, event: str, data: Any, extra_urls: Iterable[Optional[str]] = ()) -> int:
        """Deliver ``event`` to every configured URL and ``extra_urls`` in the background; returns how many."""
        urls = list(dict.fromkeys([*self.urls, *(u for u in extra_urls if u)]))
        if not urls:
            return 0
        body = self.body(event, data)
        headers = self.headers(event, body)
        for url in urls:
            task = asyncio.get_running_loop().create_task(self._deliver(event, url, body, headers))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(urls)

    async def _post(self, url: str, body: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """One attempt: ``{ok, status, error}``, never raising."""
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await self._client.post(url, content=body, headers=headers)
        except Exception as e:  # connection errors, timeouts
            return {"ok": False, "status": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        ok = response.status_code < 400
        return {"ok": ok, "status": response.status_code, "error": None if ok else f"HTTP {response.status_code}"}

    async def _deliver(self, event: str, url: str, body: bytes, headers: Dict[str, str]) -> None:
        for pause in (*RETRY_AFTER_S, None):
            outcome = await self._post(url, body, headers)
            # A 4xx means the receiver refused it: retrying will not help.
            refused = outcome["status"] is not None and 400 <= outcome["status"] < 500
            if outcome["ok"] or refused or pause is None:
                break
            await asyncio.sleep(pause)
        if not outcome["ok"]:
            logger.warning("Webhook %s to %s failed: %s", event, url, outcome["error"])

    async def test(self, url: str) -> Dict[str, Any]:
        """Send a ``test`` event once, now, and report what the URL answered."""
        body = self.body("test", {"message": "Hello from the AI toolkit"})
        return await self._post(url, body, self.headers("test", body))

    async def close(self) -> None:
        """Give deliveries under way a moment, then drop them, and close the client."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=2.0)
        for task in pending:
            task.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
