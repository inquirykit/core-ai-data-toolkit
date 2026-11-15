"""
Webhooks: a POST to each configured URL when a job or an eval run finishes.

The body is ``{"event", "time", "data"}`` as JSON, ``time`` being when the
event happened; ``X-Toolkit-Event`` names the event. Each delivery carries the
Standard Webhooks headers (https://www.standardwebhooks.com): ``webhook-id``,
the same across the retries of one message, so a receiver can drop a
duplicate; ``webhook-timestamp``, when this attempt was sent (receivers refuse
a time far from their own clock, so each attempt is signed afresh); and, with
``WEBHOOK_SECRET`` set, ``webhook-signature`` as ``v1,<base64 HMAC-SHA256 of
"id.timestamp.body">``, which any Standard Webhooks library verifies given the
secret as ``whsec_<base64 of WEBHOOK_SECRET>``. ``X-Toolkit-Signature``
(``sha256=<hex HMAC-SHA256 of the body>``) is still sent for receivers that
check it. The URLs are ``WEBHOOK_URLS`` plus the one a job, schedule or run
names itself.

Each delivery to one URL is a task of the kind "webhook" (tasks/runner.py), a
row of the tasks table, so one still waiting when the service stops goes out
at the next start, and one the service stopped under is sent again then, with
its webhook-id. Only a 2xx answer delivers it. Any other answer, or none, is
tried again RETRIES times, the wait doubling from RETRY_DELAY_S up to an hour:
for about four hours in all. 410 Gone is not tried again. Finished deliveries
are kept KEEP_DAYS, then deleted (TaskRunner.prune); GET /queue/webhooks lists
the latest (``deliveries``).
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .runner import Kind, TaskFailed, TaskRunner
from .store import FAILED

logger = logging.getLogger(__name__)

EVENTS = ("job.finished", "run.finished", "test")
# The kind of task a delivery is.
KIND = "webhook"
# Times a delivery is tried again, and the wait before the first retry, doubling up to the task runner's limit of an
# hour: 30 s, then 1, 2, 4, 8, 16 and 32 minutes, then hourly; about four hours from the first try to the last.
RETRIES = 10
RETRY_DELAY_S = 30.0
# Deliveries under way at once.
CONCURRENCY = 4
# The longest one attempt may take: the client's timeout bounds each step of a request, not the whole of it.
ATTEMPT_TIMEOUT_S = 60.0
# Days a finished delivery is kept.
KEEP_DAYS = 7
# The latest deliveries GET /queue/webhooks lists, and what it shows of each besides its message.
DELIVERIES = 20
DELIVERY_FIELDS = ("status", "error", "retried", "retries", "not_before", "created", "finished")


class Webhooks:
    def __init__(self, urls: Iterable[str] = (), secret: Optional[str] = None, timeout: float = 10.0, *,
                 tasks: TaskRunner):
        self.urls: List[str] = [u.strip() for u in urls if u and u.strip()]
        self.secret = secret or None
        self.timeout = timeout
        self._client: Optional[Any] = None  # httpx.AsyncClient, made on first use
        # Deliveries wait in the task table, so a restart does not lose one.
        self.tasks = tasks
        tasks.add(Kind(KIND, run=self._attempt, stop=self._let_finish, concurrency=CONCURRENCY,
                       finished=self._finished, on_interrupt="resume", keep_s=KEEP_DAYS * 86400))

    def describe(self) -> Dict[str, Any]:
        return {"urls": list(self.urls), "secret_set": self.secret is not None, "events": list(EVENTS)}

    def body(self, event: str, data: Any) -> bytes:
        return json.dumps({"event": event, "time": time.time(), "data": data}, default=str).encode()

    def headers(self, event: str, body: bytes, message_id: Optional[str] = None, sent_at: Optional[int] = None) -> Dict[str, str]:
        """The headers of one attempt: the message's id (new without one), the attempt's time (now without one), and
        the signatures over them and the body."""
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

    async def send(self, event: str, data: Any, extra_urls: Iterable[Optional[str]] = ()) -> int:
        """Queue ``event`` for every configured URL and ``extra_urls``, a delivery each; returns how many."""
        urls = list(dict.fromkeys([*self.urls, *(u for u in extra_urls if u)]))
        if not urls:
            return 0
        message = {"event": event, "message_id": f"msg_{uuid.uuid4().hex}", "body": self.body(event, data).decode(),
                   # The job or run it tells of, for the list of deliveries.
                   "about": data.get("id") if isinstance(data, dict) else None}
        for url in urls:
            await self.tasks.submit(KIND, ref=url, params={**message, "url": url}, retries=RETRIES,
                                    retry_delay_s=RETRY_DELAY_S, timeout_s=ATTEMPT_TIMEOUT_S)
        return len(urls)

    async def deliveries(self, limit: int = DELIVERIES) -> List[Dict[str, Any]]:
        """The latest deliveries, newest first: what each told of, where to, and how it went."""
        rows = await self.tasks.list(limit, kind=KIND)
        return [{"id": row["id"], "event": row["params"].get("event"), "about": row["params"].get("about"),
                 "url": row["params"].get("url"), **{key: row[key] for key in DELIVERY_FIELDS}} for row in rows]

    async def _attempt(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """One try at a delivery (its task's run): the status the receiver took it with, else TaskFailed, tried again
        later unless the receiver answered 410 Gone."""
        p = task["params"]
        body = p["body"].encode()
        outcome = await self._post(p["url"], body, self.headers(p["event"], body, p["message_id"]))
        if not outcome["ok"]:
            raise TaskFailed(outcome["error"], retry=outcome["status"] != 410)
        return {"status": outcome["status"]}

    @staticmethod
    async def _let_finish(task: Dict[str, Any]) -> None:
        """A cancel lets a try under way end: the client's timeout ends it soon enough."""

    async def _finished(self, task: Dict[str, Any]) -> None:
        """Log a delivery that failed for good."""
        if task["status"] == FAILED:
            p = task["params"]
            logger.warning("Webhook %s to %s failed after %d tries: %s", p["event"], p["url"], task["retried"] + 1,
                           task["error"])

    async def _post(self, url: str, body: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        """One attempt: ``{ok, status, error}``, never raising. Only a 2xx is ok; a redirect is not followed."""
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await self._client.post(url, content=body, headers=headers)
        except Exception as e:  # connection errors, timeouts
            return {"ok": False, "status": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        ok = 200 <= response.status_code < 300
        return {"ok": ok, "status": response.status_code, "error": None if ok else f"HTTP {response.status_code}"}

    async def test(self, url: str) -> Dict[str, Any]:
        """Send a ``test`` event once, now, and report what the URL answered."""
        body = self.body("test", {"message": "Hello from the AI toolkit"})
        return await self._post(url, body, self.headers("test", body))

    async def close(self) -> None:
        """Close the HTTP client. A delivery under way was stopped with the task runner; it goes out at the next start."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
