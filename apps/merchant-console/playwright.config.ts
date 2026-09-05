import { defineConfig, devices } from "@playwright/test";

/**
 * The console's end-to-end run, against the real platform.
 *
 * There is no mock mode here and there is not going to be one. Every figure this console
 * renders is read from the Commerce API through this app's own `/api/backend` proxy, and
 * a suite that stubbed those reads would assert that the console can render a fixture --
 * which is the one thing the whole application is built not to do. So the specs fetch the
 * same endpoints the page fetches and compare, and they skip with a clear message when
 * the platform is not up rather than failing in a way that reads like a regression.
 *
 * `workers: 1` because these specs read shared tenant state. Two workers paging the same
 * order list would be two readers of a moving collection, and a flake there would be
 * indistinguishable from a pagination bug.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  timeout: 45000,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:3001",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3001",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
