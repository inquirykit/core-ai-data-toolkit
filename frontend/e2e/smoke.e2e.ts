import { expect, test, type Page } from "@playwright/test";

// The pages as a user opens them, against a service of their own (e2e/serve.sh): every /admin section in the menu,
// every playground tab and the setup page load without an uncaught error, an error in the console or a reply the
// server failed (5xx). The sections and tabs are read from the page, so a new one is opened too, with no change here.

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

test("every /admin section in the menu opens", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  const menu = page.locator('nav[aria-label="Main"]');
  const hrefs = await menu.locator('a[href^="/admin#/"]').evaluateAll((links) => links.map((a) => a.getAttribute("href")!));
  expect(hrefs.length).toBeGreaterThan(5);
  for (const href of hrefs) {
    const link = menu.locator(`a[href="${href}"]`);
    await opens(seen, href, async () => {
      await link.click();
      await expect(link).toHaveAttribute("aria-current", "page");
      await settle(page);
    });
  }
  // What went wrong late, after its section had shown.
  expect(seen.problems).toEqual([]);
});

// Views a section's first page does not show, opened by their links: the editors and a case. A link that changes
// its form is one people may have kept, so this list changes with it.
const VIEWS = [
  "/admin#/servers/new",
  "/admin#/enrichers/new",
  "/admin#/enrichers/crtsh",
  "/admin#/evidence/inbox",
  "/admin#/evidence/_search?q=example",
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

test("every playground tab opens", async ({ page }) => {
  await signIn(page);
  const seen = watch(page);
  const tabs = page.getByRole("tablist").first().getByRole("tab");
  await opens(seen, "/playground", async () => {
    await page.goto("/playground");
    await expect(tabs.first()).toBeVisible();
    await settle(page);
  });
  const count = await tabs.count();
  expect(count).toBeGreaterThan(1);
  for (let i = 0; i < count; i++) {
    const tab = tabs.nth(i);
    await opens(seen, `/playground, tab ${await tab.innerText()}`, async () => {
      await tab.click();
      await expect(tab).toHaveAttribute("aria-selected", "true");
      await settle(page);
    });
  }
  expect(seen.problems).toEqual([]);
});

test("the setup page opens", async ({ page }) => {
  const seen = watch(page);
  await opens(seen, "/admin/setup", async () => {
    await page.goto("/admin/setup");
    await expect(page.locator("#root")).not.toBeEmpty();
    await settle(page);
  });
});
