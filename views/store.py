"""
The views and dashboards of the workspaces (spec.py says what they are): kept
beside each workspace rather than in it, in the service's own table
``tk_view`` (a SpecStore: db/specs.py), a row per view or dashboard, scoped
by its kind and workspace, its spec as JSON with when it was made and
changed. They go
when their workspace is dropped (``forget``, which the table store calls:
api/app.py adds it to ``TableStore.on_drop``), so a workspace made again with
the same name starts with none. Views may be made in any workspace there is,
the service's own too (a dashboard of the evals' tables); the admin writes
them, and a client key runs them, so a view is the admin's choice of what a
client may read: its SQL, like ``_query``'s, is not confined to its
workspace.

Saving a view checks what needs the database: its workspace (NotFound), its
SQL (one SELECT that DuckDB's parser takes, so it changes nothing:
GraphStore.check_sql, using no $name its params do not declare), and its
row cap and time limit, which
may lower the service's (``max_rows``, VIEW_MAX_ROWS; ``time_limit_ms``,
VIEW_TIME_LIMIT_MS), never raise them. A dashboard is saved only when every
item names a view of its workspace, and a view a dashboard shows is not
deleted (Conflict, naming the dashboards); one lock holds both, so neither
slips past the other.

Running a view (``run``) checks its parameters (spec.values), then reads at
most its row cap (and one more, to say ``truncated``, never the rest): SQL
through GraphStore.timed_read, within its time limit, past which the query
is stopped (ViewTimedOut, a 503: the service declined to spend longer, and the
same run may work later); a search or a table's rows through the table
store, as /db reads them for a client key already. Its answer is kept in
memory for the view's ``refresh_s``, per workspace, view, the view as saved
(with when it was made and last saved) and parameter values (with defaults,
as canonical JSON): runs within that time ask the database once, and runs
that arrive while one is asking wait for its answer instead of asking again.
The cache holds MAX_ENTRIES answers at most, the least recently used going
first, each at most its row cap. Saving a view again, even as it was, or
deleting it and making it again (its workspace dropped and made again
included) changes when it was saved, so no later run is answered from what
was kept before. ``clock`` (time.time) is what the answers' times and ages
are read from, so a test need not sleep.
``try_view`` runs a spec not saved yet, for the editor's preview: checked as
a save would be, never cached.

This never reads the settings: api/app.py passes VIEW_MAX_ROWS and
VIEW_TIME_LIMIT_MS.
"""

import asyncio
import functools
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from core.errors import Conflict, NotFound, Refused, Unavailable
from db import Database
from db.specs import SpecStore
from db.graph import GraphStore, QueryTimedOut, sql_parameters
from db.tables import sort_keys

from .spec import DashboardSpec, RowsQuery, RowsSource, SearchQuery, SqlSource, ViewSpec, declared, substitute, values

VIEW = "tk_view"
VIEW_KIND = "view"
DASHBOARD_KIND = "dashboard"
# Answers kept at most, across every workspace.
MAX_ENTRIES = 128
# A page of the list of views or dashboards, at most.
MAX_PAGE = 500


class ViewTimedOut(Unavailable):
    """A view's read ran out of its time limit and was stopped."""


@dataclass
class Kept:
    """A run's answer, kept for runs with the same parameters until ``expires`` (by the store's clock)."""
    answer: Dict[str, Any]
    expires: float


def _canonical(value: Any) -> str:
    """JSON that is the same for the same value: keys in order, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _columns(rows: List[Dict[str, Any]]) -> List[str]:
    """Every field of the rows, in the order first seen."""
    seen: Dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def _scope(workspace: str, kind: str) -> str:
    """A spec's scope (db/specs.py): its kind and its workspace, whose name holds no colon."""
    return f"{kind}:{workspace}"

class Views(SpecStore):
    """The views and dashboards, in ``tk_view`` (db/specs.py), each scoped by its kind and workspace; their runs
    through ``tables``. ``max_rows`` and ``time_limit_ms`` are the service's caps (VIEW_MAX_ROWS,
    VIEW_TIME_LIMIT_MS), which a view may lower."""

    TABLE = VIEW

    def __init__(self, database: Database, tables: GraphStore, max_rows: int, time_limit_ms: int,
                 clock: Callable[[], float] = time.time):
        super().__init__(database)
        self.tables = tables
        self.max_rows = max_rows
        self.time_limit_ms = time_limit_ms
        self.clock = clock
        # Held while a dashboard is saved or a view deleted, so a dashboard never names a view just deleted.
        self.naming = asyncio.Lock()
        # Held by a write and by ``forget``: a write checks its workspace is there under it, so none lands after a drop.
        self._rows_lock = threading.Lock()
        # The answers kept, least recently used first; the runs asking the database now, by the same keys.
        self._kept: "OrderedDict[Tuple, Kept]" = OrderedDict()
        self._asking: Dict[Tuple, "asyncio.Future"] = {}

    # ------------------------------------------------------------- storage
    def _check_workspace(self, workspace: str) -> None:
        if not self.tables.has_workspace(workspace):
            raise NotFound(f"No workspace named {workspace!r}")

    def _get(self, workspace: str, kind: str, name: str) -> Dict[str, Any]:
        self._check_workspace(workspace)
        found = self._find(_scope(workspace, kind), name)
        if found is None:
            raise NotFound(f"No {kind} named {name!r} in {workspace!r}")
        return found

    def _list(self, workspace: str, kind: str, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        self._check_workspace(workspace)
        return self._page(_scope(workspace, kind), limit, offset)

    def _put(self, workspace: str, kind: str, name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        # Once before the lock, so the workspaces are loaded (which takes the table store's schema lock, under which
        # ``forget`` takes this one), then again under it.
        self._check_workspace(workspace)
        with self._rows_lock:
            self._check_workspace(workspace)
            return self._save(_scope(workspace, kind), name, spec)

    def _delete(self, workspace: str, kind: str, name: str) -> None:
        with self._rows_lock:
            self._check_workspace(workspace)
            self._remove(_scope(workspace, kind), name)

    def forget(self, workspace: str) -> None:
        """Delete the workspace's views and dashboards: told by the table store once the workspace is dropped.
        Blocking."""
        with self._rows_lock:
            for kind in (VIEW_KIND, DASHBOARD_KIND):
                self._forget(_scope(workspace, kind))

    def _dashboards_showing(self, workspace: str, view: str) -> List[str]:
        return [d["name"] for d in self._all(_scope(workspace, DASHBOARD_KIND))
                if any(i.get("view") == view for i in d.get("items", []))]

    def _views_named(self, workspace: str, names: List[str]) -> List[str]:
        if not names:
            return []
        rows = self._select(f"SELECT name FROM {VIEW} WHERE scope = $s AND name = ANY($n)",
                            {"s": _scope(workspace, VIEW_KIND), "n": names})
        return [r["name"] for r in rows]

    # --------------------------------------------------------------- views
    async def list_views(self, workspace: str, limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]],
                                                                                          Optional[str]]:
        """A page of the workspace's views by name, each its spec with when it was made and changed, and the next
        page's cursor (None on the last)."""
        return await self._run(self._list, workspace, VIEW_KIND, min(limit, MAX_PAGE), offset)

    async def get_view(self, workspace: str, name: str) -> Dict[str, Any]:
        """The view's spec, with when it was made and changed; NotFound for none."""
        return await self._run(self._get, workspace, VIEW_KIND, name)

    def _check(self, workspace: str, spec: ViewSpec) -> None:
        """What a view is checked for besides its spec alone: its workspace, its caps, and its SQL. Blocking."""
        self._check_workspace(workspace)
        if spec.max_rows is not None and spec.max_rows > self.max_rows:
            raise Refused(f"max_rows is at most {self.max_rows} (VIEW_MAX_ROWS); leave it out for that many")
        source = spec.source
        if isinstance(source, SqlSource):
            if source.time_limit_ms is not None and source.time_limit_ms > self.time_limit_ms:
                raise Refused(f"time_limit_ms is at most {self.time_limit_ms} (VIEW_TIME_LIMIT_MS); leave it out for "
                              "that long")
            self.tables.check_sql(source.query)
            names = declared(spec.params)
            missing = [p for p in sql_parameters(source.query) if p not in names]
            if missing:
                raise Refused(f"The SQL uses ${missing[0]}, which is not one of the params "
                              f"({', '.join(names) or 'none'}): declare it")

    async def put_view(self, workspace: str, spec: ViewSpec) -> Dict[str, Any]:
        """Create or replace the view, checked first (``_check``)."""
        await self._run(self._check, workspace, spec)
        return await self._run(self._put, workspace, VIEW_KIND, spec.name, spec.model_dump(mode="json", by_alias=True))

    async def delete_view(self, workspace: str, name: str) -> None:
        """Delete the view; Conflict, naming them, while dashboards show it. One that is not there is no error."""
        async with self.naming:
            showing = await self._run(self._dashboards_showing, workspace, name)
            if showing:
                raise Conflict(f"The view {name} is on the dashboards {', '.join(showing)}: take it off them first")
            await self._run(self._delete, workspace, VIEW_KIND, name)

    # ---------------------------------------------------------- dashboards
    async def list_dashboards(self, workspace: str, limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]],
                                                                                               Optional[str]]:
        """A page of the workspace's dashboards by name, and the next page's cursor (None on the last)."""
        return await self._run(self._list, workspace, DASHBOARD_KIND, min(limit, MAX_PAGE), offset)

    async def get_dashboard(self, workspace: str, name: str) -> Dict[str, Any]:
        return await self._run(self._get, workspace, DASHBOARD_KIND, name)

    async def put_dashboard(self, workspace: str, spec: DashboardSpec) -> Dict[str, Any]:
        """Create or replace the dashboard: Refused unless every item names a view of the workspace."""
        async with self.naming:
            await self._run(self._check_workspace, workspace)
            wanted = list(dict.fromkeys(item.view for item in spec.items))
            found = set(await self._run(self._views_named, workspace, wanted))
            missing = [name for name in wanted if name not in found]
            if missing:
                raise Refused(f"No view named {', '.join(missing)} in {workspace!r}: a dashboard shows the "
                              "workspace's views")
            return await self._run(self._put, workspace, DASHBOARD_KIND, spec.name, spec.model_dump(mode="json"))

    async def delete_dashboard(self, workspace: str, name: str) -> None:
        """Delete the dashboard (its views stay). One that is not there is no error."""
        await self._run(self._delete, workspace, DASHBOARD_KIND, name)

    # ------------------------------------------------------------- running
    async def run(self, workspace: str, name: str, given: Dict[str, Any]) -> Dict[str, Any]:
        """Run the saved view with these parameters: ``{rows, columns, truncated, ran_at, cached}``, from what was
        kept when a run with the same values is younger than its ``refresh_s`` (``cached``), or from the run asking
        the database now with them, or asking it here."""
        saved = await self.get_view(workspace, name)
        try:
            spec = ViewSpec.model_validate({k: v for k, v in saved.items() if k not in ("created", "updated")})
        except ValidationError as e:
            raise Refused(f"The view {name} no longer fits what a view is ({e.errors()[0]['msg']}): save it again") \
                from None
        params = values(spec.params, given)
        # The view as saved holds when it was saved: a save, even of the same spec, is another key.
        key = (workspace, name, _canonical(saved), _canonical(params))
        kept = self._kept.get(key)
        if kept is not None:
            if kept.expires > self.clock():
                self._kept.move_to_end(key)
                return {**kept.answer, "cached": True}
            del self._kept[key]
        asking = self._asking.get(key)
        if asking is not None:
            return {**await asyncio.shield(asking), "cached": True}
        asking = asyncio.ensure_future(self._answer(workspace, spec, params))
        self._asking[key] = asking
        asking.add_done_callback(functools.partial(self._settle, key, spec.refresh))
        return {**await asyncio.shield(asking), "cached": False}

    def _settle(self, key: Tuple, refresh: int, asked: "asyncio.Future") -> None:
        """Keep a run's answer once it is in, unless it failed. One of a view saved again meanwhile is kept under
        the key of the view as it was, which no run asks for again: it goes as it expires, or as the least used."""
        self._asking.pop(key, None)
        if asked.cancelled() or asked.exception() is not None:
            return
        answer = asked.result()
        self._kept[key] = Kept(answer, answer["ran_at"] + refresh)
        self._kept.move_to_end(key)
        now = self.clock()
        for old in [k for k, v in self._kept.items() if v.expires <= now]:
            del self._kept[old]
        while len(self._kept) > MAX_ENTRIES:
            self._kept.popitem(last=False)

    async def try_view(self, workspace: str, spec: ViewSpec, given: Dict[str, Any]) -> Dict[str, Any]:
        """Run a spec not saved yet, checked as a save would be: the editor's preview. Never kept."""
        await self._run(self._check, workspace, spec)
        return {**await self._answer(workspace, spec, values(spec.params, given)), "cached": False}

    async def _answer(self, workspace: str, spec: ViewSpec, params: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the database: at most the view's row cap, and whether the cap cut rows."""
        cap = min(spec.max_rows or self.max_rows, self.max_rows)
        ran_at = self.clock()
        source = spec.source
        if isinstance(source, SqlSource):
            limit_ms = min(source.time_limit_ms or self.time_limit_ms, self.time_limit_ms)
            try:
                rows, truncated = await self.tables.timed_read(workspace, source.query, params, cap, limit_ms)
            except QueryTimedOut:
                raise ViewTimedOut(f"The view {spec.name} took longer than its limit of {limit_ms} ms and was "
                                   "stopped") from None
        elif isinstance(source, RowsSource):
            query = self._resolved(RowsQuery, source, params)
            limit = min(query.limit or cap, cap)
            rows, after = await self.tables.list(workspace, source.table, _filters(query.filters), sort_keys(query.sort),
                                                 limit, 0, query.search)
            # Cut by the cap only when the cap is what limited it: a limit of the view's own is its choice.
            truncated = after is not None and (query.limit is None or query.limit > cap)
        else:
            query = self._resolved(SearchQuery, source, params)
            found = await self.tables.search(workspace, source.table, vector=query.vector, text=query.text,
                                             k=min(query.k, cap + 1), filters=_filters(query.filters), words=query.q,
                                             hybrid=query.hybrid, rerank=query.rerank)
            hits = found["results"]
            rows = [{**hit["row"], "_distance": hit.get("distance"), **({"_score": hit["score"]} if "score" in hit else {})}
                    for hit in hits[:cap]]
            truncated = len(hits) > cap
        return {"rows": rows, "columns": _columns(rows), "truncated": truncated, "ran_at": ran_at}

    @staticmethod
    def _resolved(model: Any, source: Any, params: Dict[str, Any]) -> Any:
        """A search or rows source with its params in (spec.substitute), checked as what the table store takes:
        Refused, saying what is wrong, for a value that does not fit there."""
        raw = substitute(source.model_dump(by_alias=True, exclude={"kind", "table"}), params)
        # Unset options are left out, so their defaults hold.
        raw = {k: v for k, v in raw.items() if v is not None or k == "filters"}
        try:
            return model.model_validate(raw)
        except ValidationError as e:
            error = e.errors()[0]
            where = ".".join(str(p) for p in error["loc"])
            raise Refused(f"The view's {source.kind}, with these params: {where + ': ' if where else ''}{error['msg']}") \
                from None


def _filters(filters: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Filters as the table store reads them, each value as JSON, text too, so it is matched as the value it is: the
    text "null" or '"x"' as that text, where the list's query string, written as it is, would read null and x."""
    return [(name, json.dumps(value)) for name, value in filters.items()]

