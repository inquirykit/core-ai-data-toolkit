import { defineConfig, devices } from "@playwright/test";

// The smoke test (e2e/smoke.e2e.ts): the built pages in a browser, served by a service of their own (e2e/serve.sh).
// `npm run test:e2e` builds the pages first.
const port = Number(process.env.SMOKE_PORT ?? 8765);

export default defineConfig({
  testDir: "e2e",
  // Not *.test.ts, which vitest runs.
  testMatch: "*.e2e.ts",
  // Its traces and screenshots go outside the repository: nothing to ignore or commit.
  outputDir: `${process.env.TMPDIR ?? "/tmp"}/ai-toolkit-smoke-results`,
  reporter: "list",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure" },
  webServer: {
    command: "sh e2e/serve.sh",
    // Public, so it answers before anyone signs in; the service starts ArcadeDB's JVM first.
    url: `http://127.0.0.1:${port}/health`,
    timeout: 180_000,
    reuseExistingServer: false,
    stdout: "ignore",
    stderr: "pipe",
  },
});
