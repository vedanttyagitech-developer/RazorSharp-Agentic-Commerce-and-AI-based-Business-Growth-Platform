/**
 * The end-to-end suite runs against the real platform, and the config says so.
 *
 * There is no mock mode here and there was never one to switch on: `NEXT_PUBLIC_API_MODE`
 * used to be set to `"mock"` in this file and is read nowhere in `src/`, which made the
 * suite look as though it had a stubbed lane it could fall back to. It does not, by
 * design. The storefront's whole claim is that it renders what the Commerce API sent, so
 * a suite pointed at a fixture would be measuring the fixture. What the app actually needs
 * from the environment is `COMMERCE_API_URL`, which the route handler reads, and that is
 * what is passed through — defaulted to the local API rather than left to chance, so the
 * dev server the suite starts and the specs' own reads are talking to one backend.
 *
 * The suite skips itself, loudly, when that API is unreachable. Each spec pings `/healthz`
 * once and calls `test.skip` with a sentence that names the address it tried and what it
 * would have done there, because a run that fails with a selector timeout on the home page
 * teaches nobody that the backend was down.
 *
 * `fullyParallel: false` and one worker are not caution: these specs open real checkouts
 * and move real merchant prices in a shared seeded catalogue, and two of them doing that
 * at once would produce a refusal neither could explain.
 */
import { defineConfig, devices } from "@playwright/test";

const COMMERCE_API_URL = process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:3000",
    trace: "on-first-retry",
    // A refusal screen is worth looking at when an assertion about it fails.
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-390",
      use: {
        viewport: { width: 390, height: 844 },
        userAgent:
          "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
      },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    // A cold Next dev server compiles the route on first request; 60s was not enough.
    timeout: 180_000,
    env: { COMMERCE_API_URL },
  },
});
