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
 *
 * `reuseExistingServer` is the other half, and on its own `BASE_URL` is not enough: it
 * only helps the person who remembers to set it, and the failure it prevents is silent
 * for everyone who does not. With reuse off, a suite run from a second checkout against
 * the default port stops immediately with "http://localhost:3000 is already used" instead
 * of quietly measuring whichever checkout bound the port first. That is the whole point —
 * a wrong-target run must fail loudly rather than pass misleadingly, because a green run
 * that proves nothing about the branch that produced it is worse than a red one.
 *
 * The cost is that a developer with `npm run dev` already up cannot reuse it. That is the
 * right trade: the seconds this spends starting a server buy the guarantee that the code
 * under test is the code in this working tree. It was already the behaviour under CI,
 * where `!process.env.CI` evaluated to false; this makes local runs honest too.
 */
const BASE_URL = process.env.BASE_URL || "http://localhost:3000";
const PORT = new URL(BASE_URL).port || "3000";

export default defineConfig({
  testDir: "./e2e",
  /*
   * Compile the routes before anything is timed. With server reuse off, every run starts a
   * cold dev server, and a Next dev server compiles a route on its first request — so
   * without this the first spec to reach `/search` or the `/api/backend` proxy pays for a
   * bundler inside an assertion whose timeout was sized for a server round trip. It
   * asserts nothing and cannot fail the run; see the file for why.
   */
  globalSetup: "./e2e/warm-up.ts",
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
    // Never reuse. See the note on BASE_URL above: reusing whatever is on the port is how
    // a worktree's suite ends up reporting on another checkout's code.
    reuseExistingServer: false,
    // A cold Next dev server compiles the route on first request; 60s was not enough.
    timeout: 180_000,
    env: { COMMERCE_API_URL },
  },
});
