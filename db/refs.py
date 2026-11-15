"""
Refs: the tables of a workspace joined by their rows' ids, a part of
TableStore (tables.py, where the model is told). A field of type ``ref``
holds the id of a row of the table ``to``, another table of the workspace, or
with ``list`` a list of ids (schema.py). Declaring the field is all it takes: the
ids are its values, so a filter, a sort and a view's SQL join on it as on
any column.

Writes. Every id a write leaves in a ref field names a row that is there when
the write commits: checked once the write's rows are written and deleted, so
one transaction may write rows that refer to each other in any order, or
delete a row and the rows that refer to it together. A missing one refuses
the write (400).

Deletes. The rows whose ref field holds a deleted row's id follow the
field's ``on_delete``: ``restrict`` (the default) refuses the delete while
any are left (409), ``unset`` takes the id out (the field null, the id out
of its list), ``cascade`` deletes them too, and what refers to them in turn.
A table that a ref of another table names cannot be dropped until that field
goes or names another table; dropping the workspace drops them all.

The checks and cascades read and write the tables in the write's
transaction. A write that touches a joined table (one with a ref field, or
one a ref names) takes the workspace's ref lock and the gates of every joined
table (rows.py's ``_writing``); a schema change or a drop takes the lock too.
So a delete and a write naming the deleted row never pass each other unseen,
and a cascade never writes into a table whose schema is changing.

Reads. ``_expand`` follows refs from the rows a list or a get returns: a
ref field's name gives the row it names (a list of them for a list),
``table:field`` the rows of ``table`` whose ref ``field`` names the row (at
most BACK_LIMIT, the lowest ids, the parent's ``_expand_more`` naming the
steps that held more; a filtered list of that table pages them all). A dot
goes on from there (``order_items:order.product``), EXPAND_DEPTH steps at
most. What was found goes under the row's ``_expand``, by the step's name,
so the ref field keeps its id. Each step is one query for the rows of every
row before it, never one a row.
"""

import re
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.errors import TooLarge

from .engine import SEQUENCE, Conn, quote
from .schema import EMPTY, NAME, REF, Conflict, Field, TableError, TableSchema, ordered, row_type

# How many steps an expanded path takes at most (author.team.lead), and how many rows of one table a step back
# (``table:field``) gives a row at most.
EXPAND_DEPTH = 3
BACK_LIMIT = 100
# The rows one read expands to at most, every step's together: past it the read is refused, not cut.
EXPAND_MAX = 10000
# A step of an expanded path: a ref field of the table it starts from, or table:field, a ref field of that table
# naming the one it starts from.
STEP = re.compile(r"^(?:(" + NAME.pattern.strip("^$") + r"):)?([A-Za-z][A-Za-z0-9_]{0,127})$")

Row = Dict[str, Any]
# What an expanded path does, step by step: (its name, the table it reaches, the ref field it follows, whether it
# follows it back, and the steps that go on from there).
Plan = List[Tuple[str, str, Field, bool, "Plan"]]


def expand_tree(text: str) -> Dict[str, Any]:
    """``_expand``'s paths (comma-separated, steps joined by dots) as a tree of steps; TableError for a bad one."""
    tree: Dict[str, Any] = {}
    for path in text.split(","):
        steps = path.strip().split(".")
        if len(steps) > EXPAND_DEPTH:
            raise TableError(f"Expand {path.strip()!r}: {EXPAND_DEPTH} steps at most")
        node = tree
        for step in steps:
            if not STEP.match(step):
                raise TableError(f"Expand {path.strip()!r}: each step is a ref field, or table:field for the rows "
                                 "whose ref field names the row")
            node = node.setdefault(step, {})
    return tree


class Refs:
    """TableStore's ref fields: their checks, deletes and expansions (a mixin: tables.py builds the store)."""

    # ------------------------------------------------------------------ the joins
    def _ref_lock(self, workspace: str) -> threading.RLock:
        """The workspace's ref lock (the module's docstring)."""
        with self._gates_lock:
            return self._ref_locks.setdefault(workspace, threading.RLock())

    def _joined(self, workspace: str) -> set:
        """The workspace's tables with a ref field, and those a ref names."""
        joined = set()
        for name, schema in (self._load().get(workspace) or {}).items():
            for f in schema.fields.values():
                if f.type == REF:
                    joined.update((name, f.to))
        return joined

    def _referrers(self, workspace: str, table: str) -> List[Tuple[str, Field]]:
        """The ref fields of the workspace naming ``table``: (their table, the field)."""
        return [(name, f) for name, schema in self._tables(workspace).items() for f in schema.fields.values()
                if f.type == REF and f.to == table]

    def _check_ref_targets(self, workspace: str, table: str, schema: TableSchema) -> None:
        """TableError unless each ref field of the schema names another table of the workspace that is neither a
        bucket, whose rows are files, nor a kept table, whose keeper deletes rows without asking."""
        refs = [f for f in schema.fields.values() if f.type == REF]
        if refs and self.keeper(table) is not None:
            raise TableError(f"{table} is kept by {self.keeper(table)}: its fields cannot be refs")
        for f in refs:
            if f.to == table:
                raise TableError(f"{f.name}: a ref names the rows of another table, not those of {table}")
            if not self._has(workspace, f.to):
                raise TableError(f"{f.name}: there is no table {f.to!r} in {workspace!r} to refer to")
            if self._schema(workspace, f.to).bucket:
                raise TableError(f"{f.name}: {f.to} is a bucket, whose rows are files: a ref names a table's rows")
            if self.keeper(f.to) is not None:
                raise TableError(f"{f.name}: {f.to} is kept by {self.keeper(f.to)}, which deletes its rows without "
                                 "asking: a ref cannot name it")

    def _check_unreferenced(self, workspace: str, table: str) -> None:
        """Conflict while a ref field of another table names this one."""
        others = [f"{name}.{f.name}" for name, f in self._referrers(workspace, table)]
        if others:
            raise Conflict(f"{', '.join(others)} refer to {table}: remove those fields, or point them elsewhere, "
                           "before dropping it")

    # ------------------------------------------------------------------ writes
    def _dangling(self, db: Conn, workspace: str, table: str, f: Field,
                  ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Up to three (id, v) of the table's rows (those of ``ids``, or all) whose ref field ``f`` holds an id
        ``v`` its table has no row of."""
        col, params = quote(f.name), {}
        held = f"unnest({col})" if f.list else col
        among = ""
        if ids is not None:
            among, params["ids"] = " AND id = ANY($ids)", list(ids)
        return self._query(
            f"SELECT id, v FROM (SELECT id, {held} AS v FROM {row_type(workspace, table)} WHERE {col} IS NOT NULL"
            f"{among}) AS w WHERE NOT EXISTS (SELECT 1 FROM {row_type(workspace, f.to)} AS r WHERE r.id = w.v) "
            "ORDER BY id LIMIT 3", params, db)

    def _check_ref_rows(self, db: Conn, workspace: str, table: str, old: TableSchema, new: TableSchema) -> None:
        """TableError, naming a few rows, when a ref field the schema change adds (or points elsewhere) holds ids
        its table has no row of. Run once the change is applied, in its transaction."""
        for name, f in new.fields.items():
            was = old.fields.get(name)
            if f.type != REF or (was is not None and (was.type, was.list, was.to) == (f.type, f.list, f.to)):
                continue
            bad = self._dangling(db, workspace, table, f)
            if bad:
                raise TableError("Rows there now do not fit the new schema: " + "; ".join(
                    f"{r['id']} ({f.name}: {f.to} has no row {r['v']!r})" for r in bad))

    @staticmethod
    def _holding(f: Field) -> str:
        """The condition that ref field ``f`` holds one of the ids ``$ids``."""
        return f"list_has_any({quote(f.name)}, $ids)" if f.list else f"{quote(f.name)} = ANY($ids)"

    def _settle_refs(self, db: Conn, workspace: str, written: Dict[str, Sequence[str]],
                     deleted: Dict[str, Sequence[str]]) -> None:
        """Within a write's transaction, once its rows are written and deleted: the rows referring to the deleted
        rows follow their field's on_delete (cascades, in turn, until none are left), then the write is refused
        while a restrict ref still names a deleted row (Conflict) or a written row names a row that is not there
        (TableError)."""
        joined = self._joined(workspace)
        if not joined & (set(written) | set(deleted)):
            return
        gone = {table: list(dict.fromkeys(ids)) for table, ids in deleted.items() if ids and table in joined}
        cascading = dict(gone)
        while cascading:
            more: Dict[str, List[str]] = {}
            for target, ids in cascading.items():
                for table, f in self._referrers(workspace, target):
                    t = row_type(workspace, table)
                    if f.on_delete == "cascade":
                        hit = [r["id"] for r in self._query(f"SELECT id FROM {t} WHERE {self._holding(f)}",
                                                            {"ids": ids}, db)]
                        if hit:
                            self._delete_ids(db, workspace, table, hit)
                            more.setdefault(table, []).extend(hit)
                            gone.setdefault(table, []).extend(hit)
                    elif f.on_delete == "unset":
                        # The rows changed take a new write number, as any write gives them (rows.py).
                        col = quote(f.name)
                        value = f"list_filter({col}, lambda _id: NOT list_contains($ids, _id))" if f.list else "NULL"
                        db.execute(f"UPDATE {t} SET {col} = {value}, _seq = nextval('{SEQUENCE}') "
                                   f"WHERE {self._holding(f)}", {"ids": ids})
            cascading = more
        for target, ids in gone.items():
            for table, f in self._referrers(workspace, target):
                if f.on_delete != "restrict":
                    continue
                held = self._query(f"SELECT id FROM {row_type(workspace, table)} WHERE {self._holding(f)} "
                                   "ORDER BY id LIMIT 4", {"ids": ids}, db)
                if held:
                    some = ", ".join(r["id"] for r in held[:3]) + (", ..." if len(held) > 3 else "")
                    raise Conflict(f"Rows of {table} ({some}) refer to the {target} deleted, by {f.name}: delete them "
                                   f"first, or make {table}.{f.name}'s on_delete cascade or unset")
        for table, ids in written.items():
            for f in self._schema(workspace, table).fields.values():
                if f.type != REF or not ids:
                    continue
                bad = self._dangling(db, workspace, table, f, ids)
                if bad:
                    r = bad[0]
                    raise TableError(f"{f.name} of {table} row {r['id']!r}: {f.to} has no row {r['v']!r}")

    # ------------------------------------------------------------------ reads
    def _plan(self, workspace: str, table: str, tree: Dict[str, Any]) -> Plan:
        """The expanded paths of ``tree`` as steps from ``table``; TableError for a step that is not a ref."""
        tables = self._tables(workspace)
        plan: Plan = []
        for step, below in tree.items():
            other, _, name = step.rpartition(":")
            f = tables.get(other if other else table, EMPTY).fields.get(name)
            if f is None or f.type != REF or (other and f.to != table):
                forward = [n for n, g in tables.get(table, EMPTY).fields.items() if g.type == REF]
                back = [f"{t}:{g.name}" for t, g in self._referrers(workspace, table)]
                takes = ", ".join(forward + back) or "nothing: no ref names it, and it has none"
                raise TableError(f"Expand {step!r}: {table} expands {takes}")
            reached = other if other else f.to
            plan.append((step, reached, f, bool(other), self._plan(workspace, reached, below)))
        return plan

    def _expand_rows(self, workspace: str, rows: List[Row], plan: Plan, count: List[int]) -> None:
        """Put under each row's ``_expand`` what the plan's steps reach from it (the module's docstring), counting
        the rows found in ``count``: TooLarge past EXPAND_MAX."""
        if not plan or not rows:
            return
        for row in rows:
            row.setdefault("_expand", {})
        for step, reached, f, back, below in plan:
            found = self._back(workspace, rows, step, reached, f) if back else self._forward(workspace, rows, step, f)
            count[0] += len(found)
            if count[0] > EXPAND_MAX:
                raise TooLarge(f"Expanding these rows reads more than {EXPAND_MAX} rows: ask for fewer (_limit), or "
                               "expand fewer steps")
            self._expand_rows(workspace, found, below, count)

    def _forward(self, workspace: str, rows: List[Row], step: str, f: Field) -> List[Row]:
        """Each row's ``step``: the row its ref field names (the rows, in its list's order). The rows found."""
        ids: List[str] = []
        for row in rows:
            value = row.get(f.name)
            ids.extend(value if isinstance(value, list) else [value] if isinstance(value, str) else [])
        by_id = {r["id"]: r for r in self._bulk_get(workspace, f.to, list(dict.fromkeys(ids)))}
        for row in rows:
            value = row.get(f.name)
            if isinstance(value, list):
                row["_expand"][step] = [by_id[i] for i in value if i in by_id]
            elif isinstance(value, str) and value in by_id:
                row["_expand"][step] = by_id[value]
        return list(by_id.values())

    def _back(self, workspace: str, rows: List[Row], step: str, table: str, f: Field) -> List[Row]:
        """Each row's ``step``: the rows of ``table`` whose ref field names it, the lowest ids first, BACK_LIMIT at
        most (``_expand_more`` names the step when there were more). The rows found."""
        ids = [row["id"] for row in rows]
        # Up to one past the limit for each row, so a row with more is told; through a list, a row of the table once
        # for each id of its list.
        parent, source = quote(f.name), None
        if f.list:
            parent, source = "_parent", f"(SELECT *, unnest({quote(f.name)}) AS _parent FROM {row_type(workspace, table)})"
        found = self._read_rows(workspace, table, f" WHERE {parent} = ANY($ids) QUALIFY row_number() OVER "
                                f"(PARTITION BY {parent} ORDER BY id) <= $n ORDER BY id",
                                {"ids": ids, "n": BACK_LIMIT + 1}, source=source)
        of: Dict[str, Dict[str, Row]] = {i: {} for i in ids}
        for r in found:
            value = r.get(f.name)
            for i in (value if isinstance(value, list) else [value]):
                if i in of:
                    of[i].setdefault(r["id"], r)
        given: Dict[str, Row] = {}
        for row in rows:
            held = list(of[row["id"]].values())
            if len(held) > BACK_LIMIT:
                row.setdefault("_expand_more", []).append(step)
            row["_expand"][step] = [given.setdefault(r["id"], ordered(r)) for r in held[:BACK_LIMIT]]
        return list(given.values())
