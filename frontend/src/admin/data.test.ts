import { describe, expect, it } from "vitest";
import { queryString } from "../shared/api";
import {
  cellKind,
  columnsOf,
  dataHash,
  describeField,
  edgeColumns,
  edgesCypher,
  edgesMatch,
  edgeTypeQuery,
  filterQuery,
  inferField,
  listQuery,
  locationFromHash,
  nextSort,
  joinLabel,
  nodesCypher,
  nodesMatch,
  parseParams,
  parseRow,
  pinKey,
  pinnedTables,
  preview,
  queryColumns,
  querySnippets,
  searchTree,
  tableQuery,
  toCsv,
  type Namespace,
} from "./data";

describe("the data location in the hash", () => {
  it("reads the namespace and table", () => {
    expect(locationFromHash("#/data")).toEqual({ namespace: undefined, table: undefined });
    expect(locationFromHash("#/data/app")).toEqual({ namespace: "app", table: undefined });
    expect(locationFromHash("#/data/app/todo")).toEqual({ namespace: "app", table: "todo" });
    expect(locationFromHash("#/models/app")).toEqual({});
  });

  it("round-trips", () => {
    expect(locationFromHash("#/data/app/todo/schema")).toEqual({ namespace: "app", table: "todo", view: "schema" });
    expect(locationFromHash("#/data/app/todo/other")).toEqual({ namespace: "app", table: "todo" });
    for (const loc of [{}, { namespace: "app" }, { namespace: "app", table: "todo" }, { namespace: "app", table: "todo", view: "schema" as const }]) {
      expect(locationFromHash(dataHash(loc))).toEqual({ namespace: undefined, table: undefined, ...loc });
    }
  });

  it("points at an edge type's page", () => {
    expect(dataHash({ namespace: "app", edge: "works at" })).toBe("#/data/app/_edges/works%20at");
    expect(locationFromHash("#/data/app/_edges/works%20at")).toEqual({ namespace: "app", table: undefined, edge: "works at" });
    // No type named: the namespace's page.
    expect(locationFromHash("#/data/app/_edges")).toEqual({ namespace: "app", table: undefined });
    // A table's page has its rows and schema only: the old edges and graph views fall back to its rows.
    expect(locationFromHash("#/data/app/people/edges")).toEqual({ namespace: "app", table: "people" });
    expect(locationFromHash("#/data/app/people/graph?n=people%2Fann")).toEqual({ namespace: "app", table: "people" });
    expect(dataHash({ namespace: "app", table: "people", view: "rows" })).toBe("#/data/app/people");
  });

  it("points at the graph query, with the table, edge type or row to start from", () => {
    expect(dataHash({ namespace: "app", graph: true })).toBe("#/data/app/_graph");
    expect(dataHash({ namespace: "app", graph: true, graphTable: "people" })).toBe("#/data/app/_graph?t=people");
    expect(dataHash({ namespace: "app", graph: true, graphEdge: "knows" })).toBe("#/data/app/_graph?e=knows");
    expect(dataHash({ namespace: "app", graph: true, graphNode: "people/ann b" })).toBe("#/data/app/_graph?n=people%2Fann+b");
    expect(locationFromHash("#/data/app/_graph")).toEqual({ namespace: "app", table: undefined, graph: true });
    expect(locationFromHash("#/data/app/_graph?t=people&e=knows&n=people%2Fann+b")).toEqual({
      namespace: "app", table: undefined, graph: true, graphTable: "people", graphEdge: "knows", graphNode: "people/ann b",
    });
    // The graph flag wins over a table in the location.
    expect(dataHash({ namespace: "app", table: "todo", graph: true })).toBe("#/data/app/_graph");
  });

  it("points at the query panel, with the table to start from", () => {
    expect(dataHash({ namespace: "app", query: true })).toBe("#/data/app/_query");
    expect(dataHash({ namespace: "app", query: true, queryTable: "to do" })).toBe("#/data/app/_query?t=to%20do");
    expect(locationFromHash("#/data/app/_query")).toEqual({ namespace: "app", table: undefined, query: true });
    expect(locationFromHash("#/data/app/_query?t=todo")).toEqual({ namespace: "app", table: undefined, query: true, queryTable: "todo" });
    // The query flag wins over a table in the location.
    expect(dataHash({ namespace: "app", table: "todo", query: true })).toBe("#/data/app/_query");
  });
});

describe("the sidebar", () => {
  const ns = (name: string, tables: string[], edges: string[] = []): Namespace => ({
    name,
    managed: null,
    size_bytes: 0,
    edges: edges.map((e) => ({ name: e, count: 0, size_bytes: 0 })),
    tables: tables.map((t) => ({ name: t, count: 0, size_bytes: 0, strict: false, graph: false, fields: [] })),
  });
  const namespaces = [ns("app", ["people", "notes"], ["knows"]), ns("crm", ["Persons", "deals"]), ns("empty", [])];
  const names = (search: string) =>
    searchTree(namespaces, search).map((e) => `${e.namespace.name}: ${[...e.tables, ...e.edges].map((t) => t.name).join(" ")}`);

  it("searches table and edge type names, or keeps all of a namespace whose name matches", () => {
    expect(names("")).toEqual(["app: people notes knows", "crm: Persons deals", "empty: "]);
    expect(names(" PE ")).toEqual(["app: people", "crm: Persons"]);
    expect(names("crm")).toEqual(["crm: Persons deals"]);
    expect(names("emp")).toEqual(["empty: "]);
    expect(names("kno")).toEqual(["app: knows"]);
    expect(names("zzz")).toEqual([]);
  });

  it("matches the namespace and the table apart", () => {
    expect(names("app/not")).toEqual(["app: notes"]);
    expect(names("app/kn")).toEqual(["app: knows"]);
    expect(names("a.pe")).toEqual(["app: people"]);
    expect(names("/pe")).toEqual(["app: people", "crm: Persons"]);
    expect(names("em/")).toEqual(["empty: "]);
    expect(names("app/deals")).toEqual([]);
  });

  it("lists the pins it shows, in the order pinned", () => {
    const pins = ["crm/deals", "gone/table", "app/people"];
    const pinned = (search: string) => pinnedTables(searchTree(namespaces, search), pins).map((p) => pinKey(p.namespace.name, p.table.name));
    expect(pinned("")).toEqual(["crm/deals", "app/people"]);
    expect(pinned("peo")).toEqual(["app/people"]);
    expect(pinned("crm")).toEqual(["crm/deals"]);
  });
});

describe("queries", () => {
  it("lists result columns as first seen, id first", () => {
    expect(queryColumns([])).toEqual([]);
    expect(queryColumns([{ n: 1, id: "a" }, { table: "t", n: 2 }])).toEqual(["id", "n", "table"]);
  });

  it("reads parameters as a JSON object", () => {
    expect(parseParams("")).toEqual({ params: {} });
    expect(parseParams('{"id": "ann"}')).toEqual({ params: { id: "ann" } });
    expect(parseParams("[1]")).toHaveProperty("error");
    expect(parseParams("{bad")).toHaveProperty("error");
  });

  it("writes CSV with JSON for lists and objects", () => {
    expect(toCsv([{ id: "a", n: 1, tags: ["x", "y"] }, { id: "b, c", note: 'say "hi"' }])).toBe(
      'id,n,tags,note\na,1,"[""x"",""y""]",\n"b, c",,,"say ""hi"""',
    );
  });

  it("writes examples with the namespace's own names", () => {
    const tables = [
      { name: "notes", count: 0, size_bytes: 0, strict: false, graph: false, fields: [] },
      { name: "people", count: 0, size_bytes: 0, strict: false, graph: true, fields: [] },
    ];
    const sql = querySnippets("sql", tables, [{ name: "knows", count: 0, size_bytes: 0 }]);
    expect(sql[0].text).toBe("SELECT FROM notes LIMIT 100");
    expect(sql.find((s) => s.title === "A row's neighbors")?.text).toContain("both('knows')) FROM people");
    expect(querySnippets("cypher", [], [])[0].text).toBe("MATCH (n:table)\nOPTIONAL MATCH (n)-[e]-(m)\nRETURN n, e, m LIMIT 100");
    expect(querySnippets("cypher", tables, [{ name: "knows", count: 0, size_bytes: 0 }])[1].text).toBe("MATCH (a:people)-[e:knows]->(b)\nRETURN a, e, b LIMIT 100");
  });

  it("starts a table's query in SQL, a node table's and an edge type's in Cypher", () => {
    expect(tableQuery("notes")).toEqual({ language: "sql", text: "SELECT FROM notes LIMIT 100" });
    expect(tableQuery("people", true)).toEqual({ language: "cypher", text: "MATCH (n:people)\nOPTIONAL MATCH (n)-[e]-(m)\nRETURN n, e, m LIMIT 100" });
    expect(edgeTypeQuery("knows")).toBe("MATCH (a)-[e:knows]->(b)\nRETURN a, e, b LIMIT 100");
  });
});

describe("a node table's and an edge type's Cypher", () => {
  const page = { where: "", sort: null, limit: 25, skip: 0 };

  it("matches the table's nodes, or the type's edges with both ends", () => {
    expect(nodesMatch("people")).toBe("MATCH (n:people)");
    expect(edgesMatch("knows")).toBe("MATCH (a)-[e:knows]->(b)");
  });

  it("pages the nodes by id, or by a field then id", () => {
    expect(nodesCypher("people", page)).toEqual({
      rows: "MATCH (n:people)\nRETURN properties(n) AS row\nORDER BY n.id\nSKIP 0 LIMIT 25",
      count: "MATCH (n:people)\nRETURN count(n) AS count",
    });
    const sorted = nodesCypher("people", { where: " n.age > 30 ", sort: { field: "age", desc: true }, limit: 100, skip: 200 });
    expect(sorted.rows).toBe("MATCH (n:people)\nWHERE n.age > 30\nRETURN properties(n) AS row\nORDER BY n.`age` DESC, n.id\nSKIP 200 LIMIT 100");
    expect(sorted.count).toBe("MATCH (n:people)\nWHERE n.age > 30\nRETURN count(n) AS count");
    expect(nodesCypher("people", { ...page, sort: { field: "id", desc: true } }).rows).toContain("ORDER BY n.id DESC\n");
  });

  it("pages the edges by their ends unless sorted otherwise", () => {
    expect(edgesCypher("knows", page)).toEqual({
      rows: "MATCH (a)-[e:knows]->(b)\nRETURN e\nORDER BY startNode(e).id, endNode(e).id\nSKIP 0 LIMIT 25",
      count: "MATCH (a)-[e:knows]->(b)\nRETURN count(e) AS count",
    });
    const order = (field: string, desc = false) => edgesCypher("knows", { ...page, sort: { field, desc } }).rows.split("\n")[2];
    expect(order("to", true)).toBe("ORDER BY endNode(e).id DESC, startNode(e).id, endNode(e).id");
    expect(order("since")).toBe("ORDER BY e.`since`, startNode(e).id, endNode(e).id");
    expect(edgesCypher("knows", { ...page, where: "b:companies" }).count).toBe("MATCH (a)-[e:knows]->(b)\nWHERE b:companies\nRETURN count(e) AS count");
  });

  it("lists an edge's columns, the type being the page's, and names a pair of tables", () => {
    expect(edgeColumns([])).toEqual(["from", "to"]);
    expect(edgeColumns([{ type: "k", from: "a/1", to: "a/2", w: 1 }, { since: 2020, type: "k", from: "a/2", to: "a/3" }])).toEqual(["from", "to", "since", "w"]);
    expect(joinLabel({ from: "people", to: "companies" })).toBe("people → companies");
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
  const field = { name: "a", type: "string" as const, list: false, required: false, indexed: false, unique: false };

  it("describes a declaration", () => {
    expect(describeField({ ...field, required: true, indexed: true, unique: true })).toBe("string, required, unique");
    expect(describeField({ ...field, type: "integer", list: true, indexed: true })).toBe("list of integer, indexed");
    expect(describeField({ ...field, type: "any" })).toBe("any");
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
