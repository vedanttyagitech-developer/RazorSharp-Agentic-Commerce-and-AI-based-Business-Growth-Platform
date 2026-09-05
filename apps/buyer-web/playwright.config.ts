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

/**
 * Where this suite expects to find the storefront, and where it will start one.
 *
 * Parameterised rather than pinned to 3000, because `reuseExistingServer` plus a fixed
 * port is a quiet way to test the wrong code. Several checkouts of this repository can be
 * open at once — a worktree is the normal way to work on it — and each of them runs
 * `npm run dev` on 3000. The first one to bind wins, and every later suite reuses it: the
 * specs then pass or fail against a storefront built from a different working tree than
 * the one whose files are being changed, with nothing on screen to say so.
 *
 * So `BASE_URL` decides both halves. The port the dev server binds is read out of it, so
 * the server this config starts is always the server the specs talk to, and a second
 * checkout runs its own suite against its own code with `BASE_URL=http://localhost:3100`.
 */
const BASE_URL = process.env.BASE_URL || "http://localhost:3000";
const PORT = new URL(BASE_URL).port || "3000";

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
    baseURL: BASE_URL,
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
    command: `npm run dev -- --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    // A cold Next dev server compiles the route on first request; 60s was not enough.
    timeout: 180_000,
    env: { COMMERCE_API_URL },
  },
});
