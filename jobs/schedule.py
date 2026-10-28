"""
Schedules: a job submitted again and again, on a cron expression or every
so many seconds. ``Scheduler`` keeps them and, every POLL_S seconds (see
jobs/runner.py), submits the ones that are due.

Cron is the classic five fields, minute hour day-of-month month day-of-week,
each ``*``, a number, a range ``a-b``, a list ``a,b`` or a step ``*/n`` /
``a-b/n``, read in the server's local time. As in cron, when both day fields
are restricted a day matches either. A schedule never overlaps itself: a run
that comes due while the previous job from the schedule is still queued or
running is skipped.
"""

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from errors import Refused
from models.events import PRIORITIES

from . import registry
from .store import JobNotFound, JobStore

logger = logging.getLogger(__name__)

FIELDS: Sequence[Tuple[str, int, int]] = (
    ("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7),
)
MIN_INTERVAL_S = 60
# Seconds between checks for schedules that are due; cron works to the minute.
POLL_S = 15.0
# A cron run this many seconds late (the service was down) is skipped: a nightly job should not start at noon.
CRON_DEADLINE_S = 3600.0
# How far ahead next_run() looks before giving up (a schedule for the 30th of February, say).
HORIZON_DAYS = 366 * 4


def _field(text: str, name: str, low: int, high: int) -> Set[int]:
    values: Set[int] = set()
    for item in text.split(","):
        step = 1
        if "/" in item:
            item, step_text = item.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"{name}: bad step in {text!r}")
            step = int(step_text)
        if item == "*":
            start, end = low, high
        elif "-" in item:
            a, b = item.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"{name}: bad range {item!r}")
            start, end = int(a), int(b)
        elif item.isdigit():
            start = end = int(item)
            if step != 1:
                end = high  # "5/15" means from 5 on, every 15
        else:
            raise ValueError(f"{name}: {item!r} is not a number, range, list or *")
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise ValueError(f"{name}: {item!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression: str) -> List[Set[int]]:
    """The five fields as sets of allowed values; ValueError with the reason when it is not cron."""
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("a cron expression has five fields: minute hour day-of-month month day-of-week")
    fields = [_field(text, name, low, high) for text, (name, low, high) in zip(parts, FIELDS)]
    if 7 in fields[4]:  # Sunday, both ways
        fields[4].discard(7)
        fields[4].add(0)
    return fields


def next_run(expression: str, after: float) -> float:
    """The first time the expression matches after ``after`` (Unix seconds), to the minute, in local time."""
    minutes, hours, days, months, weekdays = parse_cron(expression)
    any_day = days == set(range(1, 32))
    any_weekday = weekdays == set(range(0, 7))
    start = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.replace(hour=0, minute=0)
    for _ in range(HORIZON_DAYS):
        if day.month in months:
            # cron's weekday has Sunday as 0; Python's Monday as 0.
            weekday = (day.weekday() + 1) % 7
            day_ok, weekday_ok = day.day in days, weekday in weekdays
            matches = (day_ok or weekday_ok) if not (any_day or any_weekday) else (day_ok and weekday_ok)
            if matches:
                for hour in sorted(hours):
                    for minute in sorted(minutes):
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate >= start:
                            return candidate.timestamp()
        day += timedelta(days=1)
    raise ValueError("the expression never matches")


def describe(cron: str = None, interval_s: float = None) -> str:
    if cron:
        return f"cron {cron}"
    if interval_s:
        for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
            if interval_s % size == 0:
                return f"every {int(interval_s // size)}{unit}"
        return f"every {int(interval_s)}s"
    return "never"


def compute_next(cron: str = None, interval_s: float = None, after: float = None) -> float:
    after = time.time() if after is None else after
    if cron:
        return next_run(cron, after)
    if interval_s:
        return after + interval_s
    raise ValueError("a schedule needs cron or interval_s")


class Scheduler:
    """The schedules in ``store``; due ones are handed to ``submit`` (JobRunner.submit)."""

    def __init__(self, store: JobStore, submit: Callable[..., Awaitable[Dict[str, Any]]]):
        self.store = store
        self.submit = submit

    def check(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """A schedule's fields checked: the job and its params, and exactly one of cron / interval_s.
        Refused for any of it that is wrong."""
        job = registry.get(body["job"])  # UnknownJob (a Refused) for a name that is not a job
        cron, interval_s = body.get("cron") or None, body.get("interval_s") or None
        try:
            params = job.cls.Params.model_validate(body.get("params") or {}).model_dump(mode="json")
            if bool(cron) == bool(interval_s):
                raise ValueError("give cron or interval_s, not both")
            if cron:
                parse_cron(cron)
            elif interval_s < MIN_INTERVAL_S:
                raise ValueError(f"interval_s must be at least {MIN_INTERVAL_S}")
            priority = body.get("priority") or "normal"
            if priority not in PRIORITIES:
                raise ValueError("priority must be high, normal or low")
        except ValueError as e:  # the params' ValidationError, or the cadence
            raise Refused(str(e)) from e
        return {
            "name": (body.get("name") or "").strip() or job.name,
            "job": job.name,
            "params": params,
            "cron": cron,
            "interval_s": interval_s,
            "enabled": bool(body.get("enabled", True)),
            "webhook_url": (body.get("webhook_url") or "").strip() or None,
            # The jobs it submits wait among the other jobs by it (jobs/tasks.py).
            "priority": priority,
        }

    async def _with_status(self, row: Dict[str, Any]) -> Dict[str, Any]:
        last = await self.store.get(row["last_job_id"]) if row.get("last_job_id") else None
        return {**row, "last_status": last["status"] if last else None}

    @staticmethod
    def _next(fields: Dict[str, Any], now: float) -> Optional[float]:
        return compute_next(fields["cron"], fields["interval_s"], now) if fields["enabled"] else None

    async def list(self) -> List[Dict[str, Any]]:
        return [await self._with_status(row) for row in await self.store.schedules()]

    async def get(self, id: str) -> Dict[str, Any]:
        row = await self.store.schedule(id)
        if row is None:
            raise JobNotFound("No such schedule")
        return await self._with_status(row)

    async def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        fields = self.check(body)
        now = time.time()
        row = {**fields, "id": uuid.uuid4().hex[:12], "created": now, "updated": now, "next_run": self._next(fields, now),
               "last_run": None, "last_job_id": None}
        logger.info("Schedule %s (%s): %s", row["id"], row["name"], describe(row["cron"], row["interval_s"]))
        return await self._with_status(await self.store.put_schedule(row))

    async def update(self, id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Replace a schedule's fields; its next run is worked out from now."""
        current = await self.get(id)
        fields = self.check({**current, **body})
        now = time.time()
        return await self._with_status(await self.store.put_schedule({**current, **fields, "updated": now,
                                                                      "next_run": self._next(fields, now)}))

    async def delete(self, id: str) -> None:
        await self.get(id)
        await self.store.delete_schedule(id)

    async def run(self, id: str) -> Dict[str, Any]:
        """Submit the schedule's job now, whatever its cadence; the next run stays as planned."""
        row = await self.get(id)
        job = await self.submit(row["job"], row["params"], schedule_id=id, webhook_url=row["webhook_url"],
                                priority=row["priority"])
        await self.store.mark_schedule_run(id, time.time(), job["id"], row["next_run"])
        return job

    async def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Submit every schedule that is due; returns the jobs submitted. A run missed while the service was down
        is made once; a schedule whose previous job is still going, or a cron run more than CRON_DEADLINE_S late,
        is skipped until its next time."""
        now = time.time() if now is None else now
        submitted = []
        for row in await self.store.due_schedules(now):
            try:
                next_run = compute_next(row["cron"], row["interval_s"], now)
            except ValueError as e:
                logger.warning("Schedule %s (%s) has no next run: %s", row["id"], row["name"], e)
                await self.store.mark_schedule_run(row["id"], now, None, None)
                continue
            late = now - row["next_run"]
            if row["cron"] and late > CRON_DEADLINE_S:
                logger.info("Schedule %s (%s): its run is %.0f minutes late, skipped until the next", row["id"],
                            row["name"], late / 60)
                await self.store.mark_schedule_run(row["id"], now, None, next_run)
                continue
            if await self.store.active_for_schedule(row["id"]):
                logger.info("Schedule %s (%s): previous job still running, skipped", row["id"], row["name"])
                await self.store.mark_schedule_run(row["id"], now, None, next_run)
                continue
            try:
                job = await self.submit(row["job"], row["params"], schedule_id=row["id"], webhook_url=row["webhook_url"],
                                        priority=row["priority"])
            except ValueError as e:
                logger.warning("Schedule %s (%s) could not submit its job: %s", row["id"], row["name"], e)
                await self.store.mark_schedule_run(row["id"], now, None, next_run)
                continue
            await self.store.mark_schedule_run(row["id"], now, job["id"], next_run)
            submitted.append(job)
        return submitted
