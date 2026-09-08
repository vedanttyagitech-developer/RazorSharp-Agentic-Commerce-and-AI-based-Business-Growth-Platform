/**
 * Container entrypoint for the two Next.js services (buyer-web, merchant-console).
 *
 * It exists for one reason, and it is the same reason infra/docker/entrypoint.py exists
 * for the Python services: the GKE Secret Manager add-on mounts secrets as *files* and
 * cannot sync them into Kubernetes Secrets or environment variables. A Next standalone
 * server reads `process.env` and nothing else, so without this shim the only way to give
 * these two processes their credentials would be to write the values into a manifest as
 * environment literals -- which is precisely what specification 21.8 and this repository's
 * own rules forbid.
 *
 * The contract is deliberately identical to the Python entrypoint's, so one SecretProvider
 * class shape and one runbook paragraph cover all four workloads:
 *
 *   * each file in APP_SECRETS_DIR is named after the environment variable it carries
 *     (`SESSION_COOKIE_SECRET`, `SCENARIO_KEY`, ...);
 *   * a variable already present in the environment wins, so `docker run -e` and a laptop
 *     `npm start` behave exactly as they do today;
 *   * only the trailing newline is stripped -- a hand-edited secret version tends to carry
 *     one and nothing else about the value is assumed;
 *   * names are logged, values never are.
 *
 * Files whose names are not environment-variable-shaped are skipped rather than exported.
 * The CSI driver and Kubernetes' atomic writer keep `..data` and `..YYYY_MM_DD` symlink
 * directories alongside the real files; those are not secrets and must not become
 * variables.
 *
 * After loading, it imports the standalone server in this same process. There is no exec:
 * node is already PID 1 under the distroless entrypoint, so SIGTERM from Kubernetes
 * reaches Next's own handler directly and graceful shutdown is unaffected.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const DEFAULT_SECRETS_DIR = "/var/run/secrets/app";
const ENV_NAME = /^[A-Z][A-Z0-9_]*$/;

/** Export every secret file in `directory`. Returns the names exported, for the log line. */
function loadSecretFiles(directory) {
  let entries;
  try {
    entries = readdirSync(directory).sort();
  } catch (cause) {
    if (cause.code === "ENOENT") {
      // No mount is the normal case on a laptop and in `docker run`. Not an error.
      return [];
    }
    // Anything else is a mount this container cannot read, and it must not look like a
    // mount that is not there. The usual cause is a directory whose mode or owner does
    // not admit the runtime user -- this image runs as 65532 -- and the consequence of
    // swallowing it is the worst kind of boot: the server starts, every secret falls back
    // to its development default, and nothing anywhere says so. `SCENARIO_KEY` becomes
    // "local-demo-scenario-key" and `OPERATOR_COOKIE_SECRET` becomes a fresh random value
    // that changes on every restart, silently signing out every operator on each rollout.
    //
    // Throwing is the right answer rather than a warning. A process that was given
    // credentials it cannot read has been misconfigured, and starting anyway converts a
    // deployment mistake into a security posture nobody chose.
    throw new Error(
      `entrypoint: cannot read the secret directory ${directory} (${cause.code}). ` +
        "The mount exists but this process cannot list it; check its mode and owner " +
        "against the container's runtime user.",
      { cause },
    );
  }

  const loaded = [];
  for (const name of entries) {
    if (name.startsWith(".")) continue;
    const path = join(directory, name);
    if (!statSync(path).isFile()) continue;
    if (!ENV_NAME.test(name)) {
      console.error(`entrypoint: ignoring ${JSON.stringify(name)}: not an environment variable name`);
      continue;
    }
    if (name in process.env) {
      console.error(`entrypoint: ${name} already set in the environment; file ignored`);
      continue;
    }
    const value = readFileSync(path, "utf8").replace(/\r?\n$/, "");
    if (value === "") {
      console.error(`entrypoint: ${name} is empty; not exported`);
      continue;
    }
    process.env[name] = value;
    loaded.push(name);
  }
  return loaded;
}

const names = loadSecretFiles(process.env.APP_SECRETS_DIR ?? DEFAULT_SECRETS_DIR);
if (names.length > 0) {
  console.error(`entrypoint: loaded ${names.length} secret(s) from files: ${names.join(", ")}`);
}

// Dynamic, and after the loading above: a static import is hoisted and would start the
// server before its credentials existed. `./server.js` is CommonJS in Next's standalone
// output, which `import()` handles.
await import("./server.js");
