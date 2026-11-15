import { describe, expect, it } from "vitest";
import { type FieldInfo, type TableInfo } from "../data";
import { BOX, boxRows, columnsOf, diagramLinks, MAX_FIELDS, placeTables } from "./diagram";

const ref = (name: string, to: string, list = false): FieldInfo => ({
  name, type: "ref", to, list, required: false, indexed: !list, unique: false, fulltext: false,
});
const table = (name: string, fields: FieldInfo[] = []) => ({ name, fields }) as unknown as TableInfo;

const shop = [
  table("customers"),
  table("orders", [ref("customer", "customers"), ref("tags", "tags", true)]),
  table("items", [ref("order", "orders"), ref("product", "products")]),
  table("products"),
  table("tags"),
  table("notes"),
];

describe("a workspace's diagram", () => {
  it("draws its table refs and its relationships' joins", () => {
    const links = diagramLinks(shop, [{ name: "knows", joins: [{ from: "customers", to: "customers" }, { from: "customers", to: "gone" }] }]);
    expect(links.map((l) => [l.kind, l.from, l.field ?? "", l.to, l.label])).toEqual([
      ["ref", "orders", "customer", "customers", "one"],
      ["ref", "orders", "tags", "tags", "several"],
      ["ref", "items", "order", "orders", "one"],
      ["ref", "items", "product", "products", "one"],
      // A join of a table the workspace no longer has is left out.
      ["relationship", "customers", "", "customers", "knows"],
    ]);
  });

  it("puts a table right of the tables whose refs name it", () => {
    const links = diagramLinks(shop, []);
    const columns = columnsOf(shop.map((t) => t.name), links);
    expect(Object.fromEntries(columns)).toEqual({ items: 0, notes: 0, orders: 1, products: 1, customers: 2, tags: 2 });
    // A relationship places its tables too.
    const works = diagramLinks([table("people"), table("companies")], [{ name: "works_at", joins: [{ from: "people", to: "companies" }] }]);
    expect(Object.fromEntries(columnsOf(["companies", "people"], works))).toEqual({ companies: 1, people: 0 });
    // Refs in a ring stop at the last column.
    const ring = diagramLinks([table("a", [ref("b", "b")]), table("b", [ref("a", "a")])], []);
    expect(Math.max(...columnsOf(["a", "b"], ring).values())).toBe(1);
  });

  it("stacks a column's boxes in the order of the boxes naming them, those no line joins last", () => {
    const links = diagramLinks(shop, []);
    const place = placeTables(new Map(shop.map((t) => [t.name, boxRows(t.fields.length)])), links);
    const step = BOX.header + BOX.row + BOX.pad + BOX.rowGap;
    expect(place.get("items")).toEqual({ x: 0, y: 0 });
    expect(place.get("notes")).toEqual({ x: 0, y: BOX.header + 2 * BOX.row + BOX.pad + BOX.rowGap });
    expect(place.get("orders")).toEqual({ x: BOX.width + BOX.columnGap, y: 0 });
    expect(place.get("products")).toEqual({ x: BOX.width + BOX.columnGap, y: BOX.header + 2 * BOX.row + BOX.pad + BOX.rowGap });
    expect(place.get("customers")?.y).toBe(0);
    expect(place.get("tags")?.y).toBe(step);
  });

  it("lists a box's fields up to a limit, and one row when it has none", () => {
    expect(boxRows(0)).toBe(1);
    expect(boxRows(3)).toBe(3);
    expect(boxRows(MAX_FIELDS + 5)).toBe(MAX_FIELDS + 1);
  });
});
