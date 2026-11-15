// A workspace's diagram (DataDiagram.tsx): its tables as boxes of their
// declared fields, a line for each table ref from its field to the table it
// names, and one for each relationship (edge type) between the tables it joins.
// Where the boxes go is worked out here, every time it is drawn: a table stands
// in a column right of the tables whose lines lead to it, so lines point rightwards.

import { type TableInfo } from "../data";

/** A line of the diagram: a table ref, from its field, or a relationship joining rows of two tables. */
export interface DiagramLink {
  id: string;
  kind: "ref" | "relationship";
  from: string;
  to: string;
  /** A ref's field */
  field?: string;
  /** A ref's "one" or "several", a relationship's type */
  label: string;
}

/** An edge type as GET /db/{ws}/_edges gives it: its name, and the pairs of tables its edges join. */
export interface EdgeTypeJoins {
  name: string;
  joins: { from: string; to: string }[];
}

/** The lines between the workspace's tables: its table refs, then its relationships' joins. */
export function diagramLinks(tables: TableInfo[], edgeTypes: EdgeTypeJoins[]): DiagramLink[] {
  const names = new Set(tables.map((t) => t.name));
  const refs = tables.flatMap((t) =>
    t.fields
      .filter((f) => f.type === "ref" && f.to && names.has(f.to))
      .map((f): DiagramLink => ({ id: `ref:${t.name}.${f.name}`, kind: "ref", from: t.name, to: f.to!, field: f.name, label: f.list ? "several" : "one" })),
  );
  const relationships = edgeTypes.flatMap((e) =>
    e.joins
      .filter((j) => names.has(j.from) && names.has(j.to))
      .map((j): DiagramLink => ({ id: `edge:${e.name}:${j.from}>${j.to}`, kind: "relationship", from: j.from, to: j.to, label: e.name })),
  );
  return [...refs, ...relationships];
}

/** A box's width, its header's and each field row's height, and the room between boxes (DataDiagram.tsx draws them so). */
export const BOX = { width: 260, header: 44, row: 24, pad: 8, columnGap: 120, rowGap: 40 } as const;
/** The most fields a box lists: the rest are counted. */
export const MAX_FIELDS = 12;

/** How many rows a table's box has under its header: its fields (MAX_FIELDS at most, then one saying how many more), or one saying it has none. */
export const boxRows = (fields: number): number => (fields > MAX_FIELDS ? MAX_FIELDS + 1 : Math.max(fields, 1));

/**
 * Each table's column: the most lines leading to it one after another (a table none leads to is in the first), so a
 * line points to a column on its right. Lines that go round in a ring stop at the last column.
 */
export function columnsOf(tables: string[], links: DiagramLink[]): Map<string, number> {
  const column = new Map(tables.map((t) => [t, 0]));
  const between = links.filter((l) => l.from !== l.to);
  for (let round = 0; round < tables.length; round++) {
    let moved = false;
    for (const link of between) {
      const next = column.get(link.from)! + 1;
      if (next > column.get(link.to)! && next < tables.length) {
        column.set(link.to, next);
        moved = true;
      }
    }
    if (!moved) break;
  }
  return column;
}

/**
 * Where each table's box goes, given how many rows each has: its column (columnsOf), and down the column in the
 * order of the boxes on its left whose lines lead to it, so that lines cross less; a table no line joins to another goes last.
 */
export function placeTables(rows: Map<string, number>, links: DiagramLink[]): Map<string, { x: number; y: number }> {
  const tables = [...rows.keys()].sort();
  const column = columnsOf(tables, links);
  const joined = new Set(links.filter((l) => l.from !== l.to).flatMap((l) => [l.from, l.to]));
  const place = new Map<string, { x: number; y: number }>();
  // The order a table has in its column, for the columns on its right to follow.
  const order = new Map<string, number>();
  const last = Math.max(0, ...column.values());
  for (let c = 0; c <= last; c++) {
    const here = tables.filter((t) => column.get(t) === c);
    const after = (t: string) => {
      const from = links.filter((l) => l.to === t && order.has(l.from)).map((l) => order.get(l.from)!);
      return from.length ? from.reduce((a, b) => a + b, 0) / from.length : Number.MAX_SAFE_INTEGER;
    };
    here.sort((a, b) => Number(!joined.has(a)) - Number(!joined.has(b)) || after(a) - after(b) || a.localeCompare(b));
    let y = 0;
    here.forEach((t, i) => {
      order.set(t, i);
      place.set(t, { x: c * (BOX.width + BOX.columnGap), y });
      y += BOX.header + rows.get(t)! * BOX.row + BOX.pad + BOX.rowGap;
    });
  }
  return place;
}
