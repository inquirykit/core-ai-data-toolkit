import { describe, expect, it } from "vitest";
import { edgeColumns, edgeOf, edgesFrom, edgesQuery, holdingSql, joinLabel, rowOf, rowsFrom, rowsQuery } from "./edges";

describe("the rows a link filters a graph table by", () => {
  const field = (name: string, list = false) => ({ name, type: "ref" as const, to: "t", list, required: false, indexed: false, unique: false, fulltext: false });
  it("hold the value in a declared field, a declared list or an undeclared field", () => {
    expect(holdingSql([field("customer")], "customer", "c'1")).toBe(`"customer" = 'c''1'`);
    expect(holdingSql([field("tags", true)], "tags", "t1")).toBe(`list_contains("tags", 't1')`);
    expect(holdingSql([], "owner", "ann")).toBe(`_rest->>'owner' = 'ann'`);
  });
});

describe("a table's and an edge type's pages in SQL", () => {
  const page = { where: "", sort: null, limit: 25, skip: 0 };

  it("say what their condition is written after", () => {
    expect(rowsFrom("people")).toBe('SELECT * FROM "people" WHERE');
    expect(edgesFrom("knows")).toBe("SELECT * FROM _edges WHERE type = 'knows' AND");
  });

  it("page the rows by id, or by a field then id, declared or not", () => {
    expect(rowsQuery("people", page, ["age"])).toEqual({
      rows: 'SELECT * EXCLUDE (_seq) FROM "people"\nWHERE true\nORDER BY id\nLIMIT 25 OFFSET 0',
      count: 'SELECT count(*) AS count FROM "people"\nWHERE true',
    });
    const sorted = rowsQuery("people", { where: " age > 30 ", sort: { field: "age", desc: true }, limit: 100, skip: 200 }, ["age"]);
    expect(sorted.rows).toBe('SELECT * EXCLUDE (_seq) FROM "people"\nWHERE (age > 30)\nORDER BY "age" DESC, id\nLIMIT 100 OFFSET 200');
    expect(sorted.count).toBe('SELECT count(*) AS count FROM "people"\nWHERE (age > 30)');
    // An undeclared field's numbers by their value, then its other values by their text, as the API sorts it.
    expect(rowsQuery("people", { ...page, sort: { field: "kind", desc: true } }, ["age"]).rows).toContain(
      "ORDER BY (CASE WHEN json_type(_rest->'kind') IN ('UBIGINT', 'BIGINT', 'DOUBLE') THEN (_rest->'kind'->>'$')::DOUBLE END) DESC, " +
        "(_rest->'kind'->>'$') DESC, id\n",
    );
    expect(rowsQuery("people", { ...page, sort: { field: "id", desc: true } }, []).rows).toContain("ORDER BY id DESC\n");
    // A table with a vector field: its rows without it.
    expect(rowsQuery("docs", page, ["embedding"], "embedding").rows).toContain('SELECT * EXCLUDE ("embedding", _seq) FROM "docs"');
  });

  it("read a row back as the API shows it, its undeclared fields among the rest", () => {
    expect(rowOf({ id: "a", name: "Ann", age: null, _rest: { kind: "lead", name: "not this" }, _seq: 7 })).toEqual({ id: "a", name: "Ann", kind: "lead" });
    expect(rowOf({ id: "b", _rest: null, _seq: 1 })).toEqual({ id: "b" });
  });

  it("page the edges by their ends unless sorted otherwise, and read one as the API shows it", () => {
    expect(edgesQuery("knows", page)).toEqual({
      rows: "SELECT * FROM _edges\nWHERE type = 'knows' AND true\nORDER BY from_table, from_id, to_table, to_id\nLIMIT 25 OFFSET 0",
      count: "SELECT count(*) AS count FROM _edges\nWHERE type = 'knows' AND true",
    });
    const order = (field: string, desc = false) => edgesQuery("knows", { ...page, sort: { field, desc } }).rows.split("\n")[2];
    expect(order("to", true)).toBe("ORDER BY to_table DESC, to_id DESC, from_table, from_id, to_table, to_id");
    expect(order("since")).toBe(
      "ORDER BY (CASE WHEN json_type(props->'since') IN ('UBIGINT', 'BIGINT', 'DOUBLE') THEN (props->'since'->>'$')::DOUBLE END), " +
        "(props->'since'->>'$'), from_table, from_id, to_table, to_id",
    );
    expect(edgesQuery("knows", { ...page, where: "to_table = 'companies'" }).count).toBe(
      "SELECT count(*) AS count FROM _edges\nWHERE type = 'knows' AND (to_table = 'companies')",
    );
    expect(edgeOf({ type: "knows", from_table: "people", from_id: "a", to_table: "people", to_id: "b", props: { since: 2020 }, _seq: 3 })).toEqual({
      type: "knows", from: "people/a", to: "people/b", since: 2020,
    });
  });

  it("lists an edge's columns, the type being the page's, and names a pair of tables", () => {
    expect(edgeColumns([])).toEqual(["from", "to"]);
    expect(edgeColumns([{ type: "k", from: "a/1", to: "a/2", w: 1 }, { since: 2020, type: "k", from: "a/2", to: "a/3" }])).toEqual(["from", "to", "since", "w"]);
    expect(joinLabel({ from: "people", to: "companies" })).toBe("people → companies");
  });
});
