import { describe, expect, it } from "vitest";
import { SAMPLES, sampleTransaction } from "./samples";

// What the server checks (vectorstore/schema.py), so a sample is never refused: each value fits its field's type,
// required fields are there, unique ones differ, ids are good, links join two rows of the sample, and one
// transaction holds it all (MAX_BATCH in api/table_routes.py).
const MAX_BATCH = 1000;

function fits(type: string | undefined, value: unknown): boolean {
  switch (type) {
    case "string":
      return typeof value === "string";
    case "integer":
      return Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    case "object":
      return typeof value === "object" && value !== null && !Array.isArray(value);
    default:
      return true;
  }
}

describe.each(Object.entries(SAMPLES))("the %s sample", (_, sample) => {
  it("has rows that fit its fields", () => {
    expect(sample.rows.length).toBeGreaterThan(0);
    for (const row of sample.rows) {
      for (const f of sample.fields) {
        const value = row[f.name];
        if (value === undefined || value === null) {
          expect(f.required, `${row.id}.${f.name}`).toBeFalsy();
          continue;
        }
        const values = f.list ? (Array.isArray(value) ? value : [Symbol()]) : [value];
        expect(values.every((v) => fits(f.type, v)), `${row.id}.${f.name} = ${JSON.stringify(value)}`).toBe(true);
      }
    }
  });

  it("has good ids, and no unique value twice", () => {
    const ids = sample.rows.map((r) => r.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.every((id) => /^[^_/][^/]*$/.test(id))).toBe(true);
    for (const f of sample.fields.filter((f) => f.unique)) {
      const values = sample.rows.map((r) => r[f.name]).filter((v) => v !== undefined);
      expect(new Set(values).size, f.name).toBe(values.length);
    }
  });

  it("links rows of its own, each pair once", () => {
    const ids = new Set(sample.rows.map((r) => r.id));
    const pairs = (sample.links?.edges ?? []).map((e) => {
      expect(ids.has(e.from) && ids.has(e.to) && e.from !== e.to, `${e.from} -> ${e.to}`).toBe(true);
      return [e.from, e.to].sort().join(" ");
    });
    expect(new Set(pairs).size).toBe(pairs.length);
  });

  it("fits one transaction", () => {
    expect(sample.rows.length + (sample.links?.edges.length ?? 0)).toBeLessThanOrEqual(MAX_BATCH);
  });
});

describe("sampleTransaction", () => {
  it("puts the rows in the table it is given, and names both ends of each link there", () => {
    const people = SAMPLES.people;
    const tx = sampleTransaction(people, "friends");
    expect(tx.puts).toEqual({ friends: people.rows });
    expect(tx.edges).toHaveLength(people.links!.edges.length);
    const first = people.links!.edges[0];
    expect(tx.edges![0]).toEqual({ type: "knows", from: `friends/${first.from}`, to: `friends/${first.to}`, since: first.since });
  });

  it("has no edges for a sample without links", () => {
    expect(sampleTransaction(SAMPLES.reviews, "reviews").edges).toEqual([]);
  });
});
