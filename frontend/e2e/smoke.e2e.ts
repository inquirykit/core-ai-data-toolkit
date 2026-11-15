import { expect, test, type Locator, type Page } from "@playwright/test";

// The pages as a user opens them, against a service of their own (e2e/serve.sh): every /admin section in the menu and
// in its sections' tabs, every tab of its playground and the setup page load without an uncaught error, an error in the
// console or a reply the server failed (5xx). The sections and tabs are read from the page, so a new one is opened too,
// with no change here.

// e2e/serve.sh gives the service this key.
const ADMIN_KEY = "smoke-admin-key";

/** What goes wrong on the page from now on, each line naming where it was (``watch.at``). */
function watch(page: Page) {
  const state = { at: "", problems: [] as string[] };
  const note = (what: string) => state.problems.push(`${state.at}: ${what}`);
  page.on("pageerror", (error) => note(`uncaught ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") note(`console ${message.text()}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 500) note(`${response.status()} ${response.request().method()} ${response.url()}`);
  });
  return state;
}

/** Until the page's loads are done (polls leave gaps long enough). */
const settle = (page: Page) => page.waitForLoadState("networkidle");

/** Open one part of a page with ``open``, which waits until it shows. A part that fails says what went wrong on the
 * page, which is why it did not show; one that shows must have had nothing go wrong. */
async function opens(seen: ReturnType<typeof watch>, at: string, open: () => Promise<void>) {
  seen.at = at;
  await test.step(at, async () => {
    try {
      await open();
    } catch (error) {
      const problems = seen.problems.length ? `\nWhat went wrong on the page:\n${seen.problems.join("\n")}` : "";
      throw new Error(`${at} did not open${problems}\n${error}`);
    }
    expect(seen.problems).toEqual([]);
  });
}

/** Sign in with the admin key on the form the page shows; it stays for the tab, the playground takes it too. */
async function signIn(page: Page) {
  await page.goto("/admin");
  const key = page.locator('input[type="password"][autocomplete="current-password"]');
  await key.fill(ADMIN_KEY);
  await key.press("Enter");
  await expect(key).toBeHidden();
}

/** The /admin links in ``where``. */
const adminLinks = (where: Locator) => where.locator('a[href^="/admin#/"]').evaluateAll((links) => links.map((a) => a.getAttribute("href")!));

test("every /admin section in the menu, and in its sections' tabs, opens", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  // The groups, and Settings apart at the bottom.
  const menu = page.locator("aside nav");
  const hrefs = await adminLinks(menu);
  expect(hrefs.length).toBeGreaterThan(5);
  const opened = new Set<string>();
  const open = async (link: Locator, href: string) => {
    opened.add(href);
    await opens(seen, href, async () => {
      await link.click();
      await expect(link).toHaveAttribute("aria-current", "page");
      await settle(page);
    });
  };
  for (const href of hrefs) {
    await open(menu.locator(`a[href="${href}"]`), href);
    // A section's tabs: links in the page's header, each a section of its own.
    const tabs = page.locator("header nav");
    for (const tab of await adminLinks(tabs)) {
      if (!opened.has(tab)) await open(tabs.locator(`a[href="${tab}"]`), tab);
    }
  }
  expect(opened).toContain("/admin#/keys");
  // What went wrong late, after its section had shown.
  expect(seen.problems).toEqual([]);
});

// The styles are read from the menu's picker, so a new one is tried too. The reload is the test of the pages' <head>,
// which sets the style before the first paint.
test("every style is picked in the menu, and kept when the page is opened again", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  const picker = page.getByRole("combobox", { name: "Style" });
  await picker.click();
  const styles = await page.getByRole("option").allTextContents();
  await page.keyboard.press("Escape");
  expect(styles.length).toBeGreaterThan(1);
  for (const style of styles) {
    await opens(seen, `the ${style} style`, async () => {
      await picker.click();
      await page.getByRole("option", { name: style, exact: true }).click();
      const html = page.locator("html");
      await expect(html).toHaveAttribute("data-style", style.toLowerCase());
      await page.reload();
      await expect(html).toHaveAttribute("data-style", style.toLowerCase());
      await expect(picker).toHaveText(style);
      await settle(page);
    });
  }
  expect(seen.problems).toEqual([]);
});

// Views a section's first page does not show, opened by their links: the editors and a case. A link that changes
// its form is one people may have kept, so this list changes with it.
const VIEWS = [
  "/admin#/servers/new",
  "/admin#/enrichers/new",
  "/admin#/enrichers/crtsh",
  "/admin#/remotes/new",
  "/admin#/remotes/new?preset=r2",
  "/admin#/evidence/inbox",
  "/admin#/evidence/_search?q=example",
  "/admin#/workflows/new",
  "/admin#/playground/text",
  "/admin#/playground?model=org/some-chat-model",
];

test("the views behind a section's links open", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  for (const view of VIEWS) {
    await opens(seen, view, async () => {
      await page.goto(view);
      await expect(page.locator("main")).not.toBeEmpty();
      await settle(page);
    });
  }
  expect(seen.problems).toEqual([]);
});

// The one form that takes a secret from the page: a hosted provider picked from its preset, its key pasted once and never
// shown again. Nothing here calls the service itself, so no network is needed.
test("a hosted provider is added with its API key, which is not shown again", async ({ page }) => {
  const secret = "sk-smoke-provider-key";
  await signIn(page);
  const seen = watch(page);
  await page.goto("/admin#/servers");
  const row = page.getByRole("row").filter({ hasText: "api.openai.com" });

  await opens(seen, "the OpenAI preset, saved with a key", async () => {
    await page.getByRole("button", { name: "OpenAI", exact: true }).click();
    // The form's code loads the first time one shows; it cannot be sent before that.
    await expect(page.getByRole("textbox", { name: "URL" })).toHaveValue("https://api.openai.com");
    await page.locator('input[autocomplete="new-password"]').fill(secret);
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await expect(row).toContainText("saved");
  });
  // Added, so it is no longer offered.
  await expect(page.getByRole("button", { name: "OpenAI", exact: true })).toBeHidden();

  await opens(seen, "its form, without the key", async () => {
    await page.getByRole("button", { name: "Edit openai" }).click();
    await expect(page.getByText("A key is saved and is not shown.")).toBeVisible();
    await expect(page.locator('input[autocomplete="new-password"]')).toHaveValue("");
    await expect(page.locator("main")).not.toContainText(secret);
  });

  await opens(seen, "the key removed", async () => {
    await page.getByRole("button", { name: "Remove key" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove" }).click();
    await expect(row).toContainText("none");
  });
  expect(seen.problems).toEqual([]);
});

test("a client key is made, shown once, opens the client routes, is limited to some tables, and is revoked", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  await page.goto("/admin#/keys");
  const name = `smoke-worker-${Date.now()}`;
  let token = "";

  await opens(seen, "a new key, its token shown once", async () => {
    await page.getByRole("textbox", { name: "Name" }).fill(name);
    await page.getByRole("button", { name: "Make key" }).click();
    token = await page.getByRole("textbox", { name: "The new key" }).inputValue();
    expect(token).toMatch(/^aitk_[0-9a-f]{8}_/);
    await expect(page.getByRole("row").filter({ hasText: name })).toContainText("Live");
  });
  expect((await page.request.get("/tasks", { headers: { Authorization: `Bearer ${token}` } })).status()).toBe(200);
  expect((await page.request.get("/admin/keys", { headers: { Authorization: `Bearer ${token}` } })).status()).toBe(401);

  await opens(seen, "the key limited to reading the default workspace", async () => {
    await page.getByRole("button", { name: "Done" }).click();
    await page.getByRole("row").filter({ hasText: name }).getByRole("button", { name: `Change what ${name} may reach` }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("checkbox", { name: "Only these tables" }).click();
    await dialog.getByRole("textbox", { name: "Workspace and table" }).fill("default.*");
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByRole("row").filter({ hasText: name })).toContainText("1 table entry");
  });
  const bearer = { Authorization: `Bearer ${token}` };
  expect((await page.request.get("/db/default", { headers: bearer })).status()).toBe(200);
  expect((await page.request.post("/db/default/smoke_rows", { headers: bearer, data: { id: "a" } })).status()).toBe(403);

  await opens(seen, "the key revoked, its token gone from the page", async () => {
    await expect(page.locator("main")).not.toContainText(token);
    await page.getByRole("row").filter({ hasText: name }).getByRole("button", { name: "Revoke" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Revoke" }).click();
    await expect(page.getByRole("row").filter({ hasText: name })).toContainText("Revoked");
  });
  expect((await page.request.get("/tasks", { headers: { Authorization: `Bearer ${token}` } })).status()).toBe(401);
  expect(seen.problems).toEqual([]);
});

// A workspace's views and dashboards hold nothing until made: these are seeded through the API, as an admin would, and
// opened as a reader would: the dashboard, then each of its views.
test("a dashboard of views and its views open", async ({ page, request }) => {
  const headers = { "X-API-Key": ADMIN_KEY };
  const ok = async (call: Promise<{ ok(): boolean; status(): number; text(): Promise<string> }>) => {
    const r = await call;
    expect(r.ok(), `${r.status()} ${await r.text()}`).toBe(true);
  };
  await request.post("/db", { headers, data: { name: "smoke_views", label: "Views of the smoke test" } });
  const rows = ["north", "south", "east", "north", "south", "north"].map((region, i) => ({ id: `o${i}`, region, year: 2025 + (i % 2), total: 10 * (i + 1) }));
  // Declared, so the view's SQL reads them as columns (a table's undeclared fields are in _rest).
  const fields = [{ name: "region", type: "string" }, { name: "year", type: "integer" }, { name: "total", type: "integer" }];
  await ok(request.put("/db/smoke_views/orders/_schema", { headers, data: { fields } }));
  await ok(request.put("/db/smoke_views/orders/_bulk", { headers, data: { rows } }));
  await ok(
    request.put("/db/smoke_views/_views/by_region", {
      headers,
      data: {
        name: "by_region",
        label: "Sales by region",
        source: { kind: "sql", query: "SELECT region, sum(total) AS total FROM orders WHERE year = $year GROUP BY region ORDER BY region" },
        params: { type: "object", properties: { year: { type: "integer", enum: [2025, 2026], default: 2025 } } },
        display: { type: "bar", x: "region", y: ["total"] },
      },
    }),
  );
  await ok(
    request.put("/db/smoke_views/_views/latest", {
      headers,
      data: { name: "latest", label: "Latest orders", source: { kind: "rows", table: "orders", sort: "-id", limit: 5 }, display: { type: "table" } },
    }),
  );
  await ok(
    request.put("/db/smoke_views/_dashboards/sales", {
      headers,
      data: {
        name: "sales",
        title: "Sales",
        params: { type: "object", properties: { year: { type: "integer", enum: [2025, 2026], default: 2025 } } },
        refresh_s: 30,
        items: [
          { view: "by_region", width: 8 },
          { view: "latest", width: 4 },
        ],
      },
    }),
  );

  await signIn(page);
  const seen = watch(page);
  await opens(seen, "the dashboard", async () => {
    await page.goto("/admin#/data/smoke_views/_dashboards/sales?year=2026");
    await expect(page.getByText("Refreshes every 30 s")).toBeVisible();
    await expect(page.locator("canvas").first()).toBeVisible();
    await expect(page.getByRole("cell", { name: "o5" })).toBeVisible();
    await settle(page);
  });
  await opens(seen, "a view of it", async () => {
    await page.goto("/admin#/data/smoke_views/_views/by_region?year=2025");
    await expect(page.getByText(/^Ran at/)).toBeVisible();
    await expect(page.locator("canvas").first()).toBeVisible();
    await settle(page);
  });
  await opens(seen, "a new view's editor", async () => {
    await page.goto("/admin#/data/smoke_views/_views/_new");
    await expect(page.getByLabel("View name")).toBeVisible();
    await settle(page);
  });
  expect(seen.problems).toEqual([]);
});

// Tables joined by a table ref, made through the API: a ref's cell shows the row it names by name, a link opening that
// row; the row named counts the rows naming it, a link to them; a row's editor picks the row its ref names; the
// workspace's diagram draws the two tables and the ref's line.
test("table refs show rows by name, link both ways, pick rows and are drawn", async ({ page, request }) => {
  const headers = { "X-API-Key": ADMIN_KEY };
  const ok = async (call: Promise<{ ok(): boolean; status(): number; text(): Promise<string> }>) => {
    const r = await call;
    expect(r.ok(), `${r.status()} ${await r.text()}`).toBe(true);
  };
  await request.post("/db", { headers, data: { name: "smoke_refs", label: "Table refs of the smoke test" } });
  await request.post("/db/smoke_refs", { headers, data: { name: "customers", fields: [{ name: "name", type: "string" }] } });
  await ok(request.put("/db/smoke_refs/customers/_bulk", { headers, data: { rows: [{ id: "c1", name: "Ada" }, { id: "c2", name: "Bo" }] } }));
  await request.post("/db/smoke_refs", { headers, data: { name: "orders", fields: [{ name: "customer", type: "ref", to: "customers" }] } });
  await ok(request.put("/db/smoke_refs/orders/_bulk", { headers, data: { rows: [{ id: "o1", customer: "c1" }, { id: "o2", customer: "c1" }] } }));

  await signIn(page);
  const seen = watch(page);
  const dialog = page.getByRole("dialog");
  await opens(seen, "the row a ref names, from its name", async () => {
    await page.goto("/admin#/data/smoke_refs/orders");
    await page.getByRole("row", { name: /o1/ }).getByRole("link", { name: "Ada" }).click();
    await expect(dialog.getByRole("heading", { name: "c1" })).toBeVisible();
    await expect(dialog.getByRole("link", { name: /orders/ })).toHaveText(/orders\s*2/);
    await settle(page);
  });
  await opens(seen, "the rows naming a row, from their count", async () => {
    await page.keyboard.press("Escape");
    await page.getByRole("row", { name: /c1/ }).getByRole("link", { name: "2" }).click();
    await expect(page).toHaveURL(/orders\?customer=c1$/);
    await expect(page.getByRole("row")).toHaveCount(3);
    await settle(page);
  });
  await opens(seen, "a row picked in a row's editor", async () => {
    await page.getByRole("row", { name: /o2/ }).click();
    await dialog.getByRole("button", { name: "Change" }).click();
    await dialog.getByLabel("Find a row of customers").fill("bo");
    await dialog.getByRole("list", { name: "Rows of customers" }).getByRole("button", { name: /Bo/ }).click();
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(dialog).toBeHidden();
    await settle(page);
  });
  expect((await (await request.get("/db/smoke_refs/orders/o2", { headers })).json()).customer).toBe("c2");
  await opens(seen, "the workspace's diagram", async () => {
    await page.goto("/admin#/data/smoke_refs");
    await page.getByRole("link", { name: "Diagram" }).click();
    await expect(page.locator(".react-flow__node")).toHaveCount(2);
    await expect(page.locator(".react-flow__edge")).toHaveCount(1);
    await page.getByTestId("rf__node-customers").click();
    await expect(page).toHaveURL(/#\/data\/smoke_refs\/customers$/);
    await settle(page);
  });
});

// A workflow made through the API, as an admin would, opened on its canvas and tried on its table's rows: the dry run's
// rows label its edges, and its node shows them in its panel.
test("a workflow opens on its canvas and its dry run labels the edges", async ({ page, request }) => {
  const headers = { "X-API-Key": ADMIN_KEY };
  await request.post("/db", { headers, data: { name: "smoke_flow", label: "Workflows of the smoke test" } });
  const rows = [5, 15, 25, 35].map((total, i) => ({ id: `r${i}`, total }));
  const put = await request.put("/db/smoke_flow/orders/_bulk", { headers, data: { rows } });
  expect(put.ok(), await put.text()).toBe(true);
  const made = await request.post("/workflows", {
    headers,
    data: {
      name: "Big orders",
      nodes: [
        { id: "A", type: "table.read", settings: { workspace: "smoke_flow", table: "orders" } },
        { id: "Q", type: "sql", settings: { workspace: "smoke_flow", query: "SELECT *, total * 2 AS doubled FROM rows" } },
        { id: "B", type: "if", settings: { condition: "total > `20`" } },
        { id: "C", type: "table.write", settings: { workspace: "smoke_flow", table: "big" } },
      ],
      edges: [
        { source: "A", target: "Q" },
        { source: "Q", target: "B" },
        { source: "B", output: "true", target: "C" },
      ],
    },
  });
  expect(made.ok(), await made.text()).toBe(true);
  const { id } = (await made.json()) as { id: string };

  await signIn(page);
  const seen = watch(page);
  await opens(seen, "the workflow's canvas", async () => {
    await page.goto(`/admin#/workflows/${id}`);
    await expect(page.locator(".react-flow__node")).toHaveCount(4);
    await expect(page.locator(".react-flow__edge")).toHaveCount(3);
    await settle(page);
  });
  await opens(seen, "its dry run", async () => {
    await page.getByRole("button", { name: "Dry run" }).click();
    await expect(page.locator(".react-flow__edge-text").filter({ hasText: "4 rows" })).toHaveCount(2);
    await expect(page.locator(".react-flow__edge-text").filter({ hasText: "2 rows" })).toBeVisible();
    await page.locator(".react-flow__node", { hasText: "IF" }).click();
    await expect(page.getByText("Rows as they left it")).toBeVisible();
    // A SQL node's query is a box of lines.
    await page.locator(".react-flow__node", { hasText: "SQL" }).click();
    await expect(page.locator("textarea")).toHaveValue(/total \* 2 AS doubled/);
    await settle(page);
  });
  expect(seen.problems).toEqual([]);
});

test("every playground tab opens", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  const tabs = page.getByRole("tablist").first().getByRole("tab");
  await opens(seen, "/admin#/playground", async () => {
    await page.goto("/admin#/playground");
    await expect(tabs.first()).toBeVisible();
    await settle(page);
  });
  const count = await tabs.count();
  expect(count).toBeGreaterThan(1);
  for (let i = 0; i < count; i++) {
    const tab = tabs.nth(i);
    await opens(seen, `/admin#/playground, tab ${await tab.innerText()}`, async () => {
      await tab.click();
      await expect(tab).toHaveAttribute("aria-selected", "true");
      await settle(page);
    });
  }
  expect(seen.problems).toEqual([]);
});

// A chat reply, streamed by the page's own answer rather than a model: its Markdown drawn, and its chart block as a
// chart, both loaded the first time a reply shows.
test("a chat reply is drawn as Markdown, with its chart", async ({ page }) => {
  const chart = { type: "bar", x: "month", y: ["sales"], rows: [{ month: "Jan", sales: 120 }, { month: "Feb", sales: 150 }] };
  const pieces = ["**Sales** by month:\n\n| month | sales |\n|---|---|\n| Jan | 120 |\n\n", "```chart\n", JSON.stringify(chart), "\n```\n"];
  const events = [...pieces.map((content) => JSON.stringify({ choices: [{ delta: { content } }] })), "[DONE]"];
  await page.route("**/v1/chat/completions", (route) => route.fulfill({ contentType: "text/event-stream", body: events.map((e) => `data: ${e}\n\n`).join("") }));
  await signIn(page);
  const seen = watch(page);
  await opens(seen, "a chat reply", async () => {
    await page.goto("/admin#/playground?model=org/some-chat-model");
    const message = page.getByRole("textbox", { name: "Message" });
    await message.fill("Sales by month?");
    await message.press("Enter");
    const log = page.getByRole("region", { name: "Conversations" });
    await expect(log.locator("strong", { hasText: "Sales" })).toBeVisible();
    await expect(log.getByRole("cell", { name: "120" })).toBeVisible();
    await expect(log.getByRole("img", { name: "Bars of sales by month" })).toBeVisible();
    await settle(page);
  });
});

test("the setup page opens", async ({ page }) => {
  const seen = watch(page);
  await opens(seen, "/admin/setup", async () => {
    await page.goto("/admin/setup");
    await expect(page.locator("#root")).not.toBeEmpty();
    await settle(page);
  });
});
