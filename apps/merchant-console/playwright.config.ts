import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

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

/**
 * This file's own directory. `__dirname` rather than `import.meta.url` because Playwright
 * transpiles the config to CommonJS before it runs, where `import.meta` does not exist.
 */
const here = __dirname;

/**
 * Two consoles, because the most valuable test in this suite needs a broken one.
 *
 * `failure-path.spec.ts` has to prove that a failed read renders a failure rather than a
 * zero, and that needs a console whose upstream is genuinely unreachable. It cannot be
 * written against the healthy server -- the proxy reads `COMMERCE_API_URL` once at module
 * load, so the upstream is a property of the process, not of a request -- and it must not
 * be written by stopping the API on :8000, which is shared with the storefront and the
 * agent surfaces while this suite runs.
 *
 * So the same build is served twice, by two `next start` processes on two ports pointed at
 * two different upstreams. One build, because the upstream is read from the environment at
 * runtime and never baked in; two processes, because `.next` is read-only to `next start`
 * and they do not contend for it.
 */
const LIVE_PORT = Number(process.env.CONSOLE_PORT ?? 3101);
const DEAD_PORT = Number(process.env.CONSOLE_DEAD_PORT ?? 3102);

/** Discard. Reserved, never served, and refuses a connection at once rather than hanging. */
const DEAD_UPSTREAM = "http://127.0.0.1:9";

/**
 * Why this suite runs the built console rather than `next dev`.
 *
 * Because it is the artifact the gate produces one step earlier: `npm run build && npm run
 * e2e`, so the suite exercises the thing that ships rather than a development server that
 * ships to nobody. It is also deterministic -- no compile-on-first-request, so a slow first
 * navigation cannot be mistaken for a slow read.
 *
 * There is a second reason it matters here, and it cost a long detour to find. `next dev`
 * does not hydrate at all when the page is opened on **`127.0.0.1`**: Turbopack's HMR
 * client cannot open its WebSocket to `ws://127.0.0.1:<port>/_next/hmr`, and until that
 * socket connects no effect in any client component runs -- so every page holds its loading
 * state and never issues a single read. On `localhost`, the same dev server, same port,
 * same moment, connects and works. Measured both ways:
 *
 *     next dev via localhost   -> 8 reads, hydrated,     0 HMR errors
 *     next dev via 127.0.0.1   -> 0 reads, not hydrated, 7 HMR errors
 *
 * `next start` has no HMR socket, so the built server is immune to this and the `baseURL`
 * below can stay on `127.0.0.1` -- which is worth keeping, because it pins the address
 * rather than leaving it to whichever of ::1 or 127.0.0.1 `localhost` resolves to today.
 * Anyone driving `next dev` by hand should use `localhost`.
 */
const BUILD_ID = join(here, ".next", "BUILD_ID");

/** The newest mtime anywhere under a directory, so a stale build can be noticed. */
function newestMtime(directory: string): number {
  let newest = 0;
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    newest = Math.max(newest, entry.isDirectory() ? newestMtime(path) : statSync(path).mtimeMs);
  }
  return newest;
}

/**
 * Build, but only when there is nothing to serve or what is there is older than `src/`.
 *
 * The gate runs `npm run build` immediately before `npm run e2e`, so this is normally two
 * `stat` calls and nothing else. It exists so `npm run e2e` on its own stays honest: a
 * suite that quietly tested last week's bundle would pass while the console was broken,
 * which is the exact failure this application is arranged to prevent.
 */
function buildIfStale(): void {
  const built = existsSync(BUILD_ID) ? statSync(BUILD_ID).mtimeMs : 0;
  if (built > newestMtime(join(here, "src"))) return;
  console.warn(
    built === 0 ? "\nNo build to serve; building.\n" : "\nThe build is older than src/; rebuilding.\n",
  );
  execFileSync("npx", ["next", "build"], { cwd: here, stdio: "inherit" });
}

buildIfStale();

/**
 * One console, on a port, pointed at an upstream.
 *
 * Never reused across runs: these servers carry their upstream in their environment, and a
 * process left listening from an earlier run could be pointed anywhere at all. A suite
 * whose failure-path server happened to be a healthy one would report the opposite of the
 * truth.
 */
function server(port: number, upstream: string) {
  return {
    command: `npx next start -p ${port}`,
    url: `http://127.0.0.1:${port}`,
    cwd: here,
    env: { COMMERCE_API_URL: upstream },
    reuseExistingServer: false,
    timeout: 120000,
  };
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  timeout: 45000,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL || `http://127.0.0.1:${LIVE_PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    server(LIVE_PORT, process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000"),
    server(DEAD_PORT, DEAD_UPSTREAM),
  ],
});
