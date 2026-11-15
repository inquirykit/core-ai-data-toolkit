import { describe, expect, it } from "vitest";
import { edgeTypeQuery, parseParams, queryColumns, querySnippets, tableEdgesQuery, tableQuery, toCsv } from "./queries";

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

  it("writes examples with the workspace's own names", () => {
    const tables = [
      { name: "notes", graph: false },
      { name: "people", graph: true },
    ];
    const sql = querySnippets("sql", tables, [{ name: "knows" }]);
    expect(sql[0].text).toBe('SELECT * FROM "notes" LIMIT 100');
    expect(sql.find((s) => s.title === "A row's neighbors")?.text).toContain(`JOIN "people" n ON n.id = e.to_id AND e.to_table = 'people'`);
    expect(sql.find((s) => s.title === "A row's neighbors")?.text).toContain("e.type = 'knows'");
    expect(querySnippets("graph", [], [])[0].text).toBe("SELECT * FROM _edges\nWHERE type = 'edge_type' LIMIT 100");
    expect(querySnippets("graph", tables, [{ name: "it's" }])[0].text).toBe("SELECT * FROM _edges\nWHERE type = 'it''s' LIMIT 100");
  });

  it("starts a table's query with its rows, and a graph table's and an edge type's with their relationships", () => {
    expect(tableQuery("notes")).toBe('SELECT * FROM "notes" LIMIT 100');
    expect(tableQuery("default")).toBe('SELECT * FROM "default" LIMIT 100');
    expect(tableEdgesQuery("people")).toBe("SELECT * FROM _edges\nWHERE from_table = 'people' OR to_table = 'people' LIMIT 100");
    expect(edgeTypeQuery("knows")).toBe("SELECT * FROM _edges\nWHERE type = 'knows' LIMIT 100");
  });
});
