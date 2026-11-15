import { describe, expect, it } from "vitest";
import { type FieldInfo, type TableInfo } from "./data";
import { backColumn, backLabel, countsSql, idsOf, namedIds, nameSql, namesSql, pickSql, referrers, tableRefs, withRef } from "./refs";

const field = (name: string, more: Partial<FieldInfo> = {}): FieldInfo => ({
  name, type: "string", list: false, required: false, indexed: false, unique: false, fulltext: false, ...more,
});

describe("the name a row is shown by", () => {
  it("is a declared name, title or label before any other text", () => {
    expect(nameSql([field("email"), field("title")])).toBe('"title"');
    expect(nameSql([field("label"), field("name")])).toBe('"name"');
  });

  it("is an undeclared one the row holds, else the first declared text field", () => {
    expect(nameSql([field("email"), field("tags", { list: true })])).toBe(`coalesce(_rest->>'name', _rest->>'title', _rest->>'label', "email")`);
    expect(nameSql([])).toBe(`coalesce(_rest->>'name', _rest->>'title', _rest->>'label')`);
    // A name declared other than as text is not one; nor is it in _rest.
    expect(nameSql([field("name", { type: "integer" }), field("title", { type: "object" }), field("label", { list: true })])).toBe("NULL");
  });
});

describe("a page's table refs", () => {
  const refs = [field("customer", { type: "ref", to: "customers" }), field("tags", { type: "ref", to: "tags", list: true })];

  it("are the declared ref fields", () => {
    expect(tableRefs([field("status"), ...refs]).map((f) => f.name)).toEqual(["customer", "tags"]);
  });

  it("name the ids they hold, each once", () => {
    expect(idsOf("c1")).toEqual(["c1"]);
    expect(idsOf(["t1", 5, "t2"])).toEqual(["t1", "t2"]);
    expect(idsOf(null)).toEqual([]);
    expect(namedIds([{ id: "a", tags: ["t1", "t2"] }, { id: "b", tags: ["t2"] }, { id: "c" }], "tags")).toEqual(["t1", "t2"]);
  });

  it("read their rows' names by id, and offer rows whose id or name holds what was typed", () => {
    expect(namesSql("customers", [field("name")], ["c1"])).toEqual({
      query: `SELECT id, CAST("name" AS VARCHAR) AS name FROM "customers" WHERE id = ANY($ids)`,
      params: { ids: ["c1"] },
    });
    const pick = pickSql("customers", [field("name")], " 50%_off\\ ");
    expect(pick.params).toEqual({ like: "%50\\%\\_off\\\\%" });
    expect(pick.query).toContain(`WHERE concat_ws(' ', id, CAST("name" AS VARCHAR)) ILIKE $like ESCAPE '\\'`);
  });
});

describe("the table refs naming a table's rows", () => {
  const table = (name: string, fields: FieldInfo[]) => ({ name, fields }) as unknown as TableInfo;
  const tables = [
    table("orders", [field("customer", { type: "ref", to: "customers" }), field("payer", { type: "ref", to: "customers" })]),
    table("posts", [field("readers", { type: "ref", to: "customers", list: true }), field("tags", { type: "ref", to: "tags", list: true })]),
    table("customers", [field("name")]),
  ];
  const named = referrers(tables, "customers");

  it("are the refs of other tables to it, a column each", () => {
    expect(named.map(backColumn)).toEqual(["orders:customer", "orders:payer", "posts:readers"]);
    // The field is named only when the table has two.
    expect(named.map((r) => backLabel(r, named))).toEqual(["orders · customer", "orders · payer", "posts"]);
  });

  it("count the rows naming each row of a page", () => {
    expect(countsSql(named[0], ["c1"])).toEqual({
      query: `SELECT named AS id, count(*) AS n FROM (SELECT "customer" AS named FROM "orders" WHERE "customer" = ANY($ids)) WHERE named = ANY($ids) GROUP BY named`,
      params: { ids: ["c1"] },
    });
    // A list counts a row once, however often it names the row.
    expect(countsSql(named[2], ["c1"]).query).toContain(`SELECT unnest(list_distinct("readers")) AS named FROM "posts" WHERE list_has_any("readers", $ids)`);
  });
});

describe("a row typed with a table ref picked", () => {
  const one = field("customer", { type: "ref", to: "customers" });
  const several = field("tags", { type: "ref", to: "tags", list: true });

  it("keeps the other fields and their order", () => {
    expect(withRef('{"id": "o1", "customer": null, "status": "new"}', one, ["c2"])).toEqual({
      text: JSON.stringify({ id: "o1", customer: "c2", status: "new" }, null, 2),
    });
    expect(withRef('{"id": "o1", "customer": "c2"}', one, [])).toEqual({ text: JSON.stringify({ id: "o1", customer: null }, null, 2) });
    expect(withRef('{"id": "p1"}', several, ["t1", "t2"])).toEqual({ text: JSON.stringify({ id: "p1", tags: ["t1", "t2"] }, null, 2) });
  });

  it("says when the text is no row", () => {
    expect(withRef("{nope", one, ["c1"])).toEqual({ error: expect.stringContaining("fix it to pick a row") });
  });
});
