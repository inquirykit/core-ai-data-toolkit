import { describe, expect, it } from "vitest";
import { queryString } from "../../shared/api";
import {
  cellKind,
  columnsOf,
  fieldAsSent,
  vectorField,
  without,
  dataHash,
  describeField,
  countQuery,
  filterQuery,
  groupOfTable,
  groupsOf,
  inferField,
  isOwn,
  keeperOf,
  listQuery,
  locationFromHash,
  NEW_ENTRY,
  nextSort,
  parseRow,
  pinKey,
  pinnedTables,
  preview,
  searchTree,
  type DataLocation,
  type FieldType,
  type TableInfo,
  type WorkspaceEntry,
} from "./data";

describe("the data location in the hash", () => {
  it("reads the workspace and table", () => {
    expect(locationFromHash("#/data")).toEqual({ workspace: undefined, table: undefined });
    expect(locationFromHash("#/data/app")).toEqual({ workspace: "app", table: undefined });
    expect(locationFromHash("#/data/app/todo")).toEqual({ workspace: "app", table: "todo" });
    expect(locationFromHash("#/models/app")).toEqual({});
  });

  it("round-trips", () => {
    expect(locationFromHash("#/data/app/todo/schema")).toEqual({ workspace: "app", table: "todo", view: "schema" });
    expect(locationFromHash("#/data/app/todo/other")).toEqual({ workspace: "app", table: "todo" });
    for (const loc of [{}, { workspace: "app" }, { workspace: "app", table: "todo" }, { workspace: "app", table: "todo", view: "schema" as const }]) {
      expect(locationFromHash(dataHash(loc))).toEqual({ workspace: undefined, table: undefined, ...loc });
    }
  });

  it("opens a table's rows at a row, or filtered by a field", () => {
    expect(dataHash({ workspace: "app", table: "customers", row: "c 1" })).toBe("#/data/app/customers?_row=c+1");
    expect(dataHash({ workspace: "app", table: "orders", filter: ["customer", "c1"] })).toBe("#/data/app/orders?customer=c1");
    for (const loc of [{ row: "c1" }, { filter: ["customer", "c/1"] as [string, string] }, { row: "o1", filter: ["customer", "c1"] as [string, string] }]) {
      expect(locationFromHash(dataHash({ workspace: "app", table: "orders", ...loc }))).toEqual({ workspace: "app", table: "orders", ...loc });
    }
    // A table's other views take neither.
    expect(dataHash({ workspace: "app", table: "orders", view: "schema", row: "o1" })).toBe("#/data/app/orders/schema");
  });

  it("points at an edge type's page", () => {
    expect(dataHash({ workspace: "app", edge: "works at" })).toBe("#/data/app/_edges/works%20at");
    expect(locationFromHash("#/data/app/_edges/works%20at")).toEqual({ workspace: "app", table: undefined, edge: "works at" });
    // No type named: the workspace's page.
    expect(locationFromHash("#/data/app/_edges")).toEqual({ workspace: "app", table: undefined });
    // A table's page has its rows and schema only: the old edges and graph views fall back to its rows.
    expect(locationFromHash("#/data/app/people/edges")).toEqual({ workspace: "app", table: "people" });
    expect(locationFromHash("#/data/app/people/graph?n=people%2Fann")).toEqual({ workspace: "app", table: "people" });
    expect(dataHash({ workspace: "app", table: "people", view: "rows" })).toBe("#/data/app/people");
  });

  it("points at the graph query, with the table, edge type or row to start from", () => {
    expect(dataHash({ workspace: "app", graph: true })).toBe("#/data/app/_graph");
    expect(dataHash({ workspace: "app", graph: true, graphTable: "people" })).toBe("#/data/app/_graph?t=people");
    expect(dataHash({ workspace: "app", graph: true, graphEdge: "knows" })).toBe("#/data/app/_graph?e=knows");
    expect(dataHash({ workspace: "app", graph: true, graphNode: "people/ann b" })).toBe("#/data/app/_graph?n=people%2Fann+b");
    expect(locationFromHash("#/data/app/_graph")).toEqual({ workspace: "app", table: undefined, graph: true });
    expect(locationFromHash("#/data/app/_graph?t=people&e=knows&n=people%2Fann+b")).toEqual({
      workspace: "app", table: undefined, graph: true, graphTable: "people", graphEdge: "knows", graphNode: "people/ann b",
    });
    // The graph flag wins over a table in the location.
    expect(dataHash({ workspace: "app", table: "todo", graph: true })).toBe("#/data/app/_graph");
  });

  it("points at the workspace's diagram", () => {
    expect(dataHash({ workspace: "app", diagram: true })).toBe("#/data/app/_diagram");
    expect(locationFromHash("#/data/app/_diagram")).toEqual({ workspace: "app", table: undefined, diagram: true });
  });

  it("points at the query panel, with the table to start from", () => {
    expect(dataHash({ workspace: "app", query: true })).toBe("#/data/app/_query");
    expect(dataHash({ workspace: "app", query: true, queryTable: "to do" })).toBe("#/data/app/_query?t=to%20do");
    expect(locationFromHash("#/data/app/_query")).toEqual({ workspace: "app", table: undefined, query: true });
    expect(locationFromHash("#/data/app/_query?t=todo")).toEqual({ workspace: "app", table: undefined, query: true, queryTable: "todo" });
    // The query flag wins over a table in the location.
    expect(dataHash({ workspace: "app", table: "todo", query: true })).toBe("#/data/app/_query");
  });

  it("points at a view or a dashboard, with its params' values", () => {
    expect(dataHash({ workspace: "app", viewName: "sales" })).toBe("#/data/app/_views/sales");
    expect(dataHash({ workspace: "app", viewName: "sales", values: { year: "2025", region: "north east" } })).toBe(
      "#/data/app/_views/sales?year=2025&region=north+east",
    );
    expect(dataHash({ workspace: "app", dashboard: "main", values: {} })).toBe("#/data/app/_dashboards/main");
    expect(dataHash({ workspace: "app", viewName: NEW_ENTRY })).toBe("#/data/app/_views/_new");
    expect(locationFromHash("#/data/app/_views/sales?year=2025&region=north+east")).toEqual({
      workspace: "app", table: undefined, viewName: "sales", values: { year: "2025", region: "north east" },
    });
    expect(locationFromHash("#/data/app/_dashboards/main")).toEqual({ workspace: "app", table: undefined, dashboard: "main", values: {} });
    const locs: DataLocation[] = [{ workspace: "app", viewName: "v", values: { a: "1", tags: '["x"]' } }, { workspace: "app", dashboard: "d", values: { q: "a&b=c" } }];
    for (const loc of locs) {
      expect(locationFromHash(dataHash(loc))).toEqual({ table: undefined, ...loc });
    }
    // No name: the workspace's page, which lists them.
    expect(locationFromHash("#/data/app/_views")).toEqual({ workspace: "app", table: undefined });
    expect(locationFromHash("#/data/app/_dashboards")).toEqual({ workspace: "app", table: undefined });
  });
});

describe("the sidebar", () => {
  const ws = (name: string, tables: string[], edges: string[] = [], label: string | null = null): WorkspaceEntry => ({
    name,
    label,
    managed: null,
    created: null,
    edges: edges.map((e) => ({ name: e })),
    tables: tables.map((t) => ({ name: t, graph: false, bucket: false, vector: null, kept: null })),
  });
  const workspaces = [ws("app", ["people", "notes"], ["knows"]), ws("crm", ["Persons", "deals"], [], "Customer records"), ws("empty", [])];
  const names = (search: string) =>
    searchTree(workspaces, search).map((e) => `${e.workspace.name}: ${[...e.tables, ...e.edges].map((t) => t.name).join(" ")}`);

  it("searches table and edge type names, or keeps all of a workspace whose name matches", () => {
    expect(names("")).toEqual(["app: people notes knows", "crm: Persons deals", "empty: "]);
    expect(names(" PE ")).toEqual(["app: people", "crm: Persons"]);
    expect(names("crm")).toEqual(["crm: Persons deals"]);
    expect(names("emp")).toEqual(["empty: "]);
    expect(names("kno")).toEqual(["app: knows"]);
    expect(names("zzz")).toEqual([]);
  });

  it("matches the workspace and the table apart", () => {
    expect(names("app/not")).toEqual(["app: notes"]);
    expect(names("app/kn")).toEqual(["app: knows"]);
    expect(names("a.pe")).toEqual(["app: people"]);
    expect(names("/pe")).toEqual(["app: people", "crm: Persons"]);
    expect(names("em/")).toEqual(["empty: "]);
    expect(names("app/deals")).toEqual([]);
  });

  it("finds a workspace by its label too", () => {
    expect(names("CUSTOMER")).toEqual(["crm: Persons deals"]);
    expect(names("records/deal")).toEqual(["crm: deals"]);
    expect(names("records/people")).toEqual([]);
  });

  it("tells the service's own from workspaces", () => {
    expect(isOwn(ws("app", []))).toBe(false);
    expect(isOwn({ ...ws("evals", ["datasets"]), managed: "evals" })).toBe(true);
  });

  it("names what keeps a table: its workspace's keeper, or the table's own", () => {
    const app = ws("app", ["notes", "evidence_cases"]);
    const [notes, cases] = app.tables;
    expect(keeperOf(app, notes)).toBeNull();
    expect(keeperOf(app)).toBeNull();
    expect(keeperOf(app, { ...cases, kept: "the evidence ledger" })).toBe("the evidence ledger");
    const evals = { ...ws("evals", ["datasets"]), managed: "evals" };
    expect(keeperOf(evals, evals.tables[0])).toBe("evals");
    expect(keeperOf(evals)).toBe("evals");
  });

  it("lists the pins it shows, in the order pinned", () => {
    const pins = ["crm/deals", "gone/table", "app/people"];
    const pinned = (search: string) => pinnedTables(searchTree(workspaces, search), pins).map((p) => pinKey(p.workspace.name, p.table.name));
    expect(pinned("")).toEqual(["crm/deals", "app/people"]);
    expect(pinned("peo")).toEqual(["app/people"]);
    expect(pinned("crm")).toEqual(["crm/deals"]);
  });
});

describe("a workspace's groups", () => {
  const table = (name: string, extra: Record<string, unknown> = {}) =>
    ({ name, count: 0, size_bytes: 0, strict: false, graph: true, bucket: false, fields: [], vector: null, kept: null, ...extra }) as TableInfo;
  const field = (name: string, type: string) =>
    ({ name, type: type as FieldType, list: false, required: false, indexed: false, unique: false, fulltext: false });
  const knows = { name: "knows", count: 1, size_bytes: 0 };
  const shown = (groups: ReturnType<typeof groupsOf>) => groups.map((g) => [g.label, ...g.tables.map((t) => t.name), ...g.edges.map((e) => e.name)]);

  it("lists tables, graph tables or plain, in Tables and the relationships in Graph", () => {
    expect(shown(groupsOf({ tables: [table("people"), table("notes", { graph: false })], edges: [knows] }))).toEqual([
      ["Tables", "people", "notes"],
      ["Graph", "knows"],
    ]);
    expect(shown(groupsOf({ tables: [], edges: [knows] }))).toEqual([["Graph", "knows"]]);
  });

  it("shows a group only when it has entries, Graph also for its Graph query", () => {
    expect(groupsOf({ tables: [], edges: [] })).toEqual([]);
    expect(shown(groupsOf({ tables: [table("people")], edges: [] }))).toEqual([["Tables", "people"]]);
    expect(shown(groupsOf({ tables: [], edges: [] }, true))).toEqual([["Graph"]]);
    expect(shown(groupsOf({ tables: [table("people")], edges: [] }, true))).toEqual([["Tables", "people"], ["Graph"]]);
  });

  it("lists a table with a vector field in Vectors and a bucket in Buckets", () => {
    const docs = table("docs", { fields: [field("text", "string"), field("embedding", "vector")], vector: "embedding" });
    const files = table("files", { bucket: true, fields: [field("name", "string")] });
    const notes = table("notes", { bucket: false, fields: [field("text", "string")] });
    expect([docs, files, notes].map(groupOfTable)).toEqual(["vectors", "buckets", "tables"]);
    expect(shown(groupsOf({ tables: [files, docs, notes], edges: [knows] }))).toEqual([
      ["Tables", "notes"],
      ["Graph", "knows"],
      ["Vectors", "docs"],
      ["Buckets", "files"],
    ]);
  });
});

describe("rows", () => {
  it("lists columns id first, then plain values, then lists and objects", () => {
    expect(columnsOf([])).toEqual(["id"]);
    expect(columnsOf([{ id: "1", b: 1, tags: [] }, { meta: { x: 1 }, a: 2, id: "2", b: 3 }])).toEqual(["id", "a", "b", "meta", "tags"]);
  });

  it("tells cell kinds apart", () => {
    expect([undefined, null, true, 1, "x", [1], { a: 1 }].map(cellKind)).toEqual([
      "missing", "null", "boolean", "number", "string", "list", "object",
    ]);
  });

  it("previews values on one line", () => {
    expect(preview({ a: [1, 2] })).toBe('{"a":[1,2]}');
    expect(preview("abcdef", 4)).toBe("abc…");
  });
});

describe("the list's query", () => {
  const q = { filters: [["tags", "shop"], ["n", "3"]] as [string, string][], sort: { field: "n", desc: true }, limit: 25 };

  it("puts filters, sort and page in the query", () => {
    expect(listQuery({ ...q, cursor: "abc" })).toEqual({ tags: ["shop"], n: ["3"], _sort: "-n", _limit: 25, _cursor: "abc" });
    expect(queryString(listQuery({ ...q, cursor: "abc" }))).toBe("tags=shop&n=3&_sort=-n&_limit=25&_cursor=abc");
    expect(listQuery({ filters: [], sort: null, limit: 100 })).toEqual({ _limit: 100 });
  });

  it("counts with the filters only, a field filtered twice under its key twice", () => {
    expect(filterQuery(q.filters)).toEqual({ tags: ["shop"], n: ["3"] });
    expect(queryString(filterQuery([["tags", "a"], ["tags", "b"]]))).toBe("tags=a&tags=b");
    expect(filterQuery([])).toEqual({});
  });

  it("adds the search to the list and the count, trimmed, when there is one", () => {
    const q = { filters: [["status", "open"]] as [string, string][], search: '  phish* -"red fox" ', sort: null, limit: 25 };
    expect(listQuery(q)).toEqual({ status: ["open"], _search: 'phish* -"red fox"', _limit: 25 });
    expect(countQuery(q)).toEqual({ status: ["open"], _search: 'phish* -"red fox"' });
    expect(countQuery({ ...q, search: " " })).toEqual({ status: ["open"] });
  });
});

describe("nextSort", () => {
  it("cycles ascending, descending, off", () => {
    const asc = nextSort(null, "n");
    expect(asc).toEqual({ field: "n", desc: false });
    const desc = nextSort(asc, "n");
    expect(desc).toEqual({ field: "n", desc: true });
    expect(nextSort(desc, "n")).toBeNull();
    expect(nextSort(desc, "m")).toEqual({ field: "m", desc: false });
  });
});

describe("fields", () => {
  const field = { name: "a", type: "string" as const, list: false, required: false, indexed: false, unique: false, fulltext: false };

  it("describes a declaration", () => {
    expect(describeField({ ...field, required: true, indexed: true, unique: true })).toBe("string, required, unique");
    expect(describeField({ ...field, type: "integer", list: true, indexed: true })).toBe("list of integer, indexed");
    expect(describeField({ ...field, type: "any" })).toBe("any");
    expect(describeField({ ...field, fulltext: true })).toBe("string, full-text");
    const vector = { ...field, name: "v", type: "vector" as const, dimensions: 384, similarity: "COSINE" as const, quantization: "NONE" as const };
    expect(describeField(vector)).toBe("vector of 384, COSINE");
    expect(describeField({ ...vector, model: "m/e", from: "text" })).toBe("vector of 384, COSINE, embedded from text by m/e");
    const ref = { ...field, type: "ref" as const, to: "customers", on_delete: "restrict" as const, indexed: true };
    expect(describeField({ ...ref, required: true })).toBe("table ref to customers, required");
    expect(describeField({ ...ref, list: true, indexed: false, on_delete: "unset" })).toBe("table ref to several rows of customers, on delete unset");
  });

  it("infers one from what the rows hold", () => {
    const infer = (types: Record<string, number>) => inferField({ name: "a", present: 1, types });
    expect(infer({ string: 9, null: 1 })).toMatchObject({ type: "string", list: false });
    expect(infer({ integer: 3, number: 2 })).toMatchObject({ type: "number" });
    expect(infer({ string: 1, integer: 1 })).toMatchObject({ type: "any" });
    expect(infer({ "list<string>": 4, list: 1 })).toMatchObject({ type: "string", list: true });
    expect(infer({ "list<string>": 1, "list<any>": 1 })).toMatchObject({ type: "any", list: true });
    expect(infer({ list: 2 })).toMatchObject({ type: "any", list: true });
    expect(infer({ object: 2 })).toMatchObject({ type: "object" });
  });
});

describe("parseRow", () => {
  it("accepts an object and drops an empty id", () => {
    expect(parseRow('{"id": "a", "x": 1}')).toEqual({ row: { id: "a", x: 1 } });
    expect(parseRow('{"id": "", "x": 1}')).toEqual({ row: { x: 1 } });
  });

  it("explains what is wrong", () => {
    expect(parseRow("{")).toHaveProperty("error");
    expect(parseRow("[1]")).toEqual({ error: "A row is a JSON object" });
    expect(parseRow('{"id": 3}')).toEqual({ error: "id is a string" });
  });
});

describe("vectors", () => {
  const base = { list: false, required: false, indexed: false, unique: false, fulltext: false };
  const raw = { ...base, name: "v", type: "vector" as const, dimensions: 3 };
  const embedded = { ...raw, model: "m/e", from: "text" };

  it("sends a prompt left empty as the model's own until the field is saved, then as none", () => {
    const field = { ...embedded, query_prompt: "", document_prompt: "d: ", dimensions: null };
    expect(fieldAsSent(field, false)).toEqual({ ...base, name: "v", type: "vector", model: "m/e", from: "text", document_prompt: "d: " });
    expect(fieldAsSent(field, true)).toEqual({ ...base, name: "v", type: "vector", model: "m/e", from: "text", query_prompt: "", document_prompt: "d: " });
    // No model, no from and no prompts.
    expect(fieldAsSent({ ...raw, from: "text", query_prompt: "q" }, true)).toEqual(raw);
  });

  it("finds a table's vector field", () => {
    expect(vectorField([{ ...base, name: "t", type: "string" }, raw])).toBe(raw);
    expect(vectorField([{ ...base, name: "t", type: "string" }])).toBeUndefined();
  });

  it("leaves the vector out of a row", () => {
    expect(without({ id: "a", v: [1], n: 2 }, "v")).toEqual({ id: "a", n: 2 });
    const row = { id: "a" };
    expect(without(row, "v")).toBe(row);
    expect(without(row, undefined)).toBe(row);
  });
});

