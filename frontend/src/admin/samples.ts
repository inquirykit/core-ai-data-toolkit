import type { Schemas } from "../shared/api";
import type { Row } from "./data";

// The sample tables the New form can start a table with: a schema, its rows and,
// for people, a relationship between them. They are written through the API a
// client uses (POST /db/{ns} with the fields, then one /_transaction). Rows come
// from short word lists and a seeded random generator, so a sample is the same
// every time.

type FieldSpec = Schemas["FieldSpec"];

/** Links between a sample's own rows, by id; the rest of each object is the link's details. */
export interface SampleLinks {
  name: string;
  edges: { from: string; to: string; [detail: string]: unknown }[];
}

export interface Sample {
  label: string;
  /** The name the form suggests for the table */
  table: string;
  /** What it holds, in the line under the form */
  hint: string;
  fields: FieldSpec[];
  rows: Row[];
  links?: SampleLinks;
}

/** A seeded generator of numbers in [0, 1) (mulberry32). */
function seeded(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const pick = <T>(random: () => number, items: readonly T[]): T => items[Math.floor(random() * items.length)];
const between = (random: () => number, low: number, high: number) => low + Math.floor(random() * (high - low + 1));
const pad = (n: number) => String(n).padStart(2, "0");

const FIRST = ["Ada", "Ben", "Chloe", "Diego", "Elena", "Farah", "George", "Hana", "Ivan", "Julia", "Kofi", "Lena", "Marco", "Nina", "Omar",
  "Priya", "Quinn", "Rosa", "Sami", "Tara", "Uma", "Victor", "Wen", "Ximena", "Yusuf", "Zoe", "Aiko", "Bruno", "Carmen", "Dev"];
const LAST = ["Silva", "Becker", "Tanaka", "Okafor", "Novak", "Haddad", "Larsen", "Moreau", "Kowalski", "Santos", "Ivanova", "Nguyen",
  "Rossi", "Fischer", "Mensah", "Patel", "Costa", "Lindqvist", "Herrera", "Kim", "Weber", "Ali", "Murphy", "Sato"];
const CITIES = ["Lisbon", "Berlin", "Toronto", "Osaka", "Nairobi", "Austin", "Melbourne", "Oslo"];
const INTERESTS = ["hiking", "cooking", "chess", "photography", "running", "music", "gardening", "travel", "reading", "cycling", "painting", "climbing"];

/** 60 people, and who knows whom: mostly people of the same city, each pair once. */
function people(): Pick<Sample, "rows" | "links"> {
  const random = seeded(7);
  const names = new Set<string>();
  const rows: Row[] = [];
  while (rows.length < 60) {
    const first = pick(random, FIRST);
    const last = pick(random, LAST);
    if (names.has(`${first} ${last}`)) continue;
    names.add(`${first} ${last}`);
    const interests = new Set(Array.from({ length: between(random, 1, 3) }, () => pick(random, INTERESTS)));
    rows.push({
      id: `p${pad(rows.length + 1)}`,
      name: `${first} ${last}`,
      email: `${first}.${last}@example.com`.toLowerCase(),
      city: pick(random, CITIES),
      age: between(random, 19, 74),
      joined: `${between(random, 2019, 2026)}-${pad(between(random, 1, 12))}-${pad(between(random, 1, 28))}`,
      interests: [...interests],
    });
  }
  const pairs = new Set<string>();
  const edges: SampleLinks["edges"] = [];
  for (const person of rows) {
    const neighbors = rows.filter((r) => r.city === person.city && r.id !== person.id);
    for (let n = between(random, 1, 3); n > 0; n--) {
      const other = random() < 0.7 && neighbors.length ? pick(random, neighbors) : pick(random, rows);
      const pair = [person.id, other.id].sort().join(" ");
      if (other.id === person.id || pairs.has(pair)) continue;
      pairs.add(pair);
      edges.push({ from: person.id, to: other.id, since: between(random, 2010, 2026) });
    }
  }
  return { rows, links: { name: "knows", edges } };
}

const KINDS: Record<string, string[]> = {
  kitchen: ["Kettle", "Toaster", "Chef's Knife", "Cutting Board", "Coffee Grinder"],
  office: ["Desk Lamp", "Notebook", "Monitor Stand", "Office Chair", "Fountain Pen"],
  outdoor: ["Tent", "Hiking Boots", "Water Bottle", "Headlamp", "Backpack"],
  audio: ["Headphones", "Speaker", "Turntable", "Microphone", "Earbuds"],
};
const STYLES = ["Classic", "Compact", "Pro", "Eco", "Travel", "Deluxe", "Mini", "Smart"];

/** 40 products of four categories, priced to the cent and rated 2.5 to 5. */
function products(): Row[] {
  const random = seeded(11);
  const names = new Set<string>();
  const rows: Row[] = [];
  while (rows.length < 40) {
    const category = pick(random, Object.keys(KINDS));
    const name = `${pick(random, STYLES)} ${pick(random, KINDS[category])}`;
    if (names.has(name)) continue;
    names.add(name);
    rows.push({
      id: `sku${1001 + rows.length}`,
      name,
      category,
      price: between(random, 5, 240) - 0.01,
      in_stock: random() < 0.8,
      rating: between(random, 25, 50) / 10,
    });
  }
  return rows;
}

// Product reviews by sentiment, for trying a text classifier; the rows take one of each in turn.
const REVIEWS: Record<string, [text: string, stars: number][]> = {
  positive: [
    ["Arrived a day early and works exactly as described.", 5],
    ["Great value for the price, I'd buy it again.", 5],
    ["Setup took five minutes and the instructions were clear.", 4],
    ["The battery easily lasts the whole week.", 5],
    ["Solid build, feels like it will last for years.", 5],
    ["Support replaced the cable without any questions.", 4],
    ["Quiet, fast and looks good on the counter.", 5],
    ["My kids love it and so do I.", 5],
    ["Much lighter than my old one, perfect for travel.", 4],
    ["Sound is crisp and the bass is surprisingly deep.", 5],
    ["Comfortable enough to wear all day.", 4],
    ["Exactly the size I needed, it fits in my bag.", 4],
  ],
  negative: [
    ["Stopped working after two weeks.", 1],
    ["The lid cracked the first time I used it.", 1],
    ["Smaller than the photos suggest and feels cheap.", 2],
    ["The battery drains overnight even when it's switched off.", 1],
    ["Took a month to arrive and the box was crushed.", 2],
    ["The app keeps disconnecting and support never replied.", 1],
    ["Way too loud, I can't use it while anyone is asleep.", 2],
    ["The instructions were missing and two screws didn't fit.", 2],
    ["It still smells of plastic after a week.", 2],
    ["Sent it back: the color was nothing like the listing.", 1],
    ["Overpriced for what you get.", 2],
    ["The strap broke on the first hike.", 1],
  ],
  neutral: [
    ["It does the job, nothing more.", 3],
    ["Works fine, though the cable could be longer.", 3],
    ["Average quality for an average price.", 3],
    ["Delivery was on time; I haven't used it much yet.", 3],
    ["Some features are handy, others feel unnecessary.", 3],
    ["Looks nicer in the photos, but it works.", 3],
    ["Bought it as a gift, so I can't say how it performs.", 3],
    ["Fine for occasional use, maybe not for every day.", 3],
    ["As far as I can tell it's the same as the previous model.", 3],
    ["The color is a bit darker than I expected.", 3],
    ["Setup was fiddly, but it has worked since.", 3],
    ["Neither better nor worse than the cheaper brands.", 3],
  ],
};

function reviews(): Row[] {
  const labels = Object.keys(REVIEWS);
  return REVIEWS.positive.flatMap((_, i) =>
    labels.map((label, j) => {
      const [text, stars] = REVIEWS[label][i];
      return { id: `r${pad(i * labels.length + j + 1)}`, text, label, stars };
    }),
  );
}

const PEOPLE = people();
const PRODUCTS = products();
const REVIEW_ROWS = reviews();

export type SampleKey = "people" | "products" | "reviews";

export const SAMPLES: Record<SampleKey, Sample> = {
  people: {
    label: "People",
    table: "people",
    hint: `${PEOPLE.rows.length} people with a name, email, city, age and interests, and ${PEOPLE.links!.edges.length} “knows” relationships between them, for the SQL and Graph queries.`,
    fields: [
      { name: "name", type: "string", required: true },
      { name: "email", type: "string", unique: true },
      { name: "city", type: "string", indexed: true },
      { name: "age", type: "integer", indexed: true },
      { name: "joined", type: "string" },
      { name: "interests", type: "string", list: true },
    ],
    ...PEOPLE,
  },
  products: {
    label: "Products",
    table: "products",
    hint: `${PRODUCTS.length} products with a category, price, rating and whether they are in stock.`,
    fields: [
      { name: "name", type: "string", required: true },
      { name: "category", type: "string", indexed: true },
      { name: "price", type: "number", indexed: true },
      { name: "in_stock", type: "boolean" },
      { name: "rating", type: "number" },
    ],
    rows: PRODUCTS,
  },
  reviews: {
    label: "Reviews",
    table: "reviews",
    hint: `${REVIEW_ROWS.length} short product reviews labeled positive, negative or neutral, with their stars: a small set for trying a text classifier.`,
    fields: [
      { name: "text", type: "string", required: true },
      { name: "label", type: "string", required: true, indexed: true },
      { name: "stars", type: "integer" },
    ],
    rows: REVIEW_ROWS,
  },
};

/** The transaction that fills ``table`` with the sample: its rows, and its links as edges between them. */
export function sampleTransaction(sample: Sample, table: string): Schemas["Transaction"] {
  const links = sample.links;
  return {
    puts: { [table]: sample.rows },
    edges: (links?.edges ?? []).map(({ from, to, ...details }) => ({ type: links!.name, from: `${table}/${from}`, to: `${table}/${to}`, ...details })),
  };
}
