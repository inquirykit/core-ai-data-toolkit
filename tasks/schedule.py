"""
Schedules: a task submitted again and again, on a cron expression or every
so many seconds. ``Scheduler`` keeps them and, every POLL_S seconds (an
every() loop api/app.py starts), submits the ones that are due.

A schedule names the kind of task it submits and what that kind runs (a job's
name, for "job"). Each kind that can be scheduled declares itself once
(``Scheduler.add``): how a schedule's name and params are checked, and how its
task is submitted. The schedules live with the tasks (store.py), so any kind
can be scheduled without importing the others.

Cron is the classic five fields, minute hour day-of-month month day-of-week,
each ``*``, a number, a range ``a-b``, a list ``a,b`` or a step ``*/n`` /
``a-b/n``, read in the server's local time. As in cron, when both day fields
are restricted a day matches either. A schedule never overlaps itself: a run
that comes due while the previous task from the schedule is still queued or
running is skipped.
"""

import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from pydantic import ConfigDict, Field

from core.errors import NotFound, Refused

from core.priorities import Priority
from .runner import Retries
from .store import TaskStore

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


def check_cadence(cron: Optional[str], interval_s: Optional[float]) -> None:
    """ValueError, saying why, unless exactly one of ``cron`` (five fields that match some time) and ``interval_s``
    (at least MIN_INTERVAL_S) is given: what a schedule's cadence must be, checked before anything keeps it."""
    if bool(cron) == bool(interval_s):
        raise ValueError("give cron or interval_s, not both")
    if cron:
        next_run(cron, time.time())  # parses it, and finds that it matches
    elif interval_s < MIN_INTERVAL_S:
        raise ValueError(f"interval_s must be at least {MIN_INTERVAL_S}")


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


def following(cron: Optional[str], interval_s: Optional[float], due: float, now: float) -> float:
    """The run after one that came due at ``due`` and was handled at ``now``. Cron's is its next time after now. An
    interval's stays on its own grid, ``due`` plus whole intervals, the first after now: counted from now, each run
    would start as late as the tick that found it (up to POLL_S) and the schedule would drift; runs missed while the
    service was down are not made one by one."""
    if cron or not interval_s:
        return compute_next(cron, interval_s, now)
    return due + (math.floor((now - due) / interval_s) + 1) * interval_s


class ScheduleSpec(Retries):
    """A schedule as it is given: what it submits (``job``, the name its kind knows it by, with its ``params``), on a
    cron expression or every ``interval_s`` seconds, and the priority, webhook, retries and timeout of what it submits.
    Its kind says the rest (Schedulable.check)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field("", max_length=240, description="Shown in the dashboard; what it runs when empty")
    job: str = Field(..., min_length=1, max_length=128, description="What it runs: a job's name, for a job")
    params: Dict[str, Any] = Field(default_factory=dict)
    cron: Optional[str] = Field(None, max_length=120, description="minute hour day-of-month month day-of-week, local time")
    interval_s: Optional[float] = Field(None, ge=1, allow_inf_nan=False,
                                        description="Seconds between runs (at least 60), instead of cron")
    enabled: bool = True
    webhook_url: Optional[str] = Field(None, max_length=2000, description="POSTed to when what it submits finishes")
    priority: Priority = Field("normal", description="The priority of what it submits")


@dataclass(frozen=True)
class Schedulable:
    """A kind of task that can be scheduled.

    ``check(name, params)`` gives back the name and params a schedule keeps, checked (a ValueError, or a Refused,
    for what is wrong). ``submit(name, params, **fields)`` queues the task and returns its row; ``fields`` are the
    schedule's ``schedule_id``, ``webhook_url``, ``priority``, ``retries``, ``retry_delay_s`` and ``timeout_s``.
    """

    kind: str
    check: Callable[[str, Dict[str, Any]], Tuple[str, Dict[str, Any]]]
    submit: Callable[..., Awaitable[Dict[str, Any]]]


class Scheduler:
    """The schedules in ``store``; a due one is submitted by its kind's ``Schedulable``."""

    def __init__(self, store: TaskStore):
        self.store = store
        self.kinds: Dict[str, Schedulable] = {}

    def add(self, schedulable: Schedulable) -> None:
        self.kinds[schedulable.kind] = schedulable

    def check(self, kind: str, spec: ScheduleSpec) -> Dict[str, Any]:
        """A schedule's fields as kept, checked as its spec's fields cannot be alone: its kind, what it runs and the
        params (by the kind), and exactly one of cron and interval_s. Refused for any of it that is wrong."""
        if kind not in self.kinds:
            raise Refused(f"Tasks of kind {kind!r} cannot be scheduled here")
        cron, interval_s = spec.cron or None, spec.interval_s or None
        try:
            name, params = self.kinds[kind].check(spec.job, spec.params)
            check_cadence(cron, interval_s)
        except ValueError as e:  # the params' ValidationError, or the cadence
            raise Refused(str(e)) from e
        # The tasks it submits wait among the others of their kind by its priority (runner.py).
        return {**spec.model_dump(), "name": spec.name.strip() or name, "kind": kind, "job": name, "params": params,
                "cron": cron, "interval_s": interval_s, "webhook_url": (spec.webhook_url or "").strip() or None}

    async def _with_status(self, row: Dict[str, Any]) -> Dict[str, Any]:
        last = await self.store.get(row["last_task_id"]) if row.get("last_task_id") else None
        return {**row, "last_status": last["status"] if last else None}

    @staticmethod
    def _next(fields: Dict[str, Any], now: float) -> Optional[float]:
        return compute_next(fields["cron"], fields["interval_s"], now) if fields["enabled"] else None

    async def list(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every schedule, or those of ``kind``."""
        return [await self._with_status(row) for row in await self.store.schedules()
                if kind is None or row["kind"] == kind]

    async def get(self, id: str, kind: Optional[str] = None) -> Dict[str, Any]:
        """The schedule; NotFound for none, or, with ``kind``, for one of another kind."""
        row = await self.store.schedule(id)
        if row is None or (kind is not None and row["kind"] != kind):
            raise NotFound("No such schedule")
        return await self._with_status(row)

    async def create(self, spec: ScheduleSpec, kind: str = "job") -> Dict[str, Any]:
        """A new schedule of ``kind``, checked (``check``)."""
        fields = self.check(kind, spec)
        now = time.time()
        row = {**fields, "id": uuid.uuid4().hex[:12], "created": now, "updated": now, "next_run": self._next(fields, now),
               "last_run": None, "last_task_id": None}
        logger.info("Schedule %s (%s): %s", row["id"], row["name"], describe(row["cron"], row["interval_s"]))
        return await self._with_status(await self.store.put_schedule(row))

    async def update(self, id: str, spec: ScheduleSpec) -> Dict[str, Any]:
        """Replace a schedule's fields with ``spec``'s, its kind kept. Its next run stays as planned unless its cadence
        changed or it was switched on or off, when it is worked out from now: else each save would push it a whole
        period out."""
        current = await self.get(id)
        fields = self.check(current["kind"], spec)
        now = time.time()
        same = all(fields[k] == current[k] for k in ("cron", "interval_s", "enabled"))
        next_run = current["next_run"] if same and current["next_run"] is not None else self._next(fields, now)
        return await self._with_status(await self.store.put_schedule({**current, **fields, "updated": now,
                                                                      "next_run": next_run}))

    async def delete(self, id: str) -> None:
        await self.get(id)
        await self.store.delete_schedule(id)

    async def run(self, id: str) -> Dict[str, Any]:
        """Submit the schedule's task now, whatever its cadence; the next run stays as planned. Refused when its kind
        cannot be scheduled here (no kind of that name is declared)."""
        row = await self.get(id)
        if row["kind"] not in self.kinds:
            raise Refused(f"Tasks of kind {row['kind']!r} cannot be scheduled here")
        task = await self._submit(row)
        await self.store.mark_schedule_run(id, time.time(), task["id"], row["next_run"])
        return task

    def _submit(self, row: Dict[str, Any]) -> Awaitable[Dict[str, Any]]:
        """Submit the schedule's task, with its priority, webhook and retries."""
        return self.kinds[row["kind"]].submit(
            row["job"], row["params"], schedule_id=row["id"], webhook_url=row["webhook_url"], priority=row["priority"],
            retries=row["retries"], retry_delay_s=row["retry_delay_s"], timeout_s=row["timeout_s"])

    async def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Submit every schedule that is due; returns the tasks submitted. A run missed while the service was down
        is made once; a schedule whose previous task is still going, or a cron run more than CRON_DEADLINE_S late,
        is skipped until its next time. One whose kind cannot be scheduled here (none of that name is declared) is left
        as it is."""
        now = time.time() if now is None else now
        submitted = []
        for row in await self.store.due_schedules(now):
            if row["kind"] not in self.kinds:
                continue
            try:
                next_run = following(row["cron"], row["interval_s"], row["next_run"], now)
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
                logger.info("Schedule %s (%s): its previous task is still going, skipped", row["id"], row["name"])
                await self.store.mark_schedule_run(row["id"], now, None, next_run)
                continue
            try:
                task = await self._submit(row)
            except ValueError as e:
                logger.warning("Schedule %s (%s) could not submit its task: %s", row["id"], row["name"], e)
                await self.store.mark_schedule_run(row["id"], now, None, next_run)
                continue
            await self.store.mark_schedule_run(row["id"], now, task["id"], next_run)
            submitted.append(task)
        return submitted
