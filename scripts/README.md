# scripts

Six files. Three of them create database state, and it matters which database each one
touches, so that is the first column.

| Script | Database | What it is for |
| --- | --- | --- |
| `bootstrap_local.sh` | `commerce_dev` **and** `commerce_test` | One command from a clean machine to a working local setup. Creates both databases if absent, applies the migrations to each, installs the restricted login roles, and then verifies that every one of them is `NOSUPERUSER` and `NOBYPASSRLS` and can actually connect. Idempotent and non-destructive. |
| `seed_demo_tenant.py` | `commerce_dev` only | The one tenant and one merchant the demonstration needs. Refuses to run against `commerce_test`. Idempotent: re-running writes nothing and prints the same identifiers. |
| `seed_demo_state.py` | `commerce_dev` only | The orders, refunds, dead letters and refusals `seed_demo_tenant.py` deliberately does not create. Drives the real paths against the running API and worker rather than inserting rows: an order exists only where verified capture evidence put it there. Convergent, so re-running writes nothing; `--reset` rebuilds from empty. See `docs/DEMO.md` section 2. |
| `run_demo.sh` | reads `.env`, targets `commerce_dev` | Starts the API and the durable worker together and stops both cleanly on Ctrl-C. `--api-only` and `--worker-only` are what `make api` and `make worker` call. |
| `setup_ci_db.py` | `commerce_test` | The CI equivalent of `bootstrap_local.sh`, driven by `DATABASE_URL_TEST_ADMIN`. Called from `.github/workflows/ci.yml`. Do not use it locally; `bootstrap_local.sh` covers both databases. |
| `validate_infra.sh` | none | Static checks over the Kubernetes and Docker manifests. |
| `bootstrap_dev_roles.sql`, `bootstrap_test_roles.sql` | applied by the two scripts above | The grants and, more importantly, the revocations. These are what the tenant-isolation suites prove. |

Run them through `make` rather than directly — `make bootstrap`, `make seed`,
`make demo` — so the environment is assembled the same way every time. `make help` lists
everything.

## Two rules these scripts exist to enforce

**A login role must never be `SUPERUSER` or `BYPASSRLS`.** PostgreSQL superusers bypass
row-level security unconditionally. An isolation suite run as one passes while proving
nothing, and the failure is silent. `bootstrap_local.sh` checks all six roles on every
run and refuses to report success without them.

**A seeder may not write a financial row.** `seed_demo_state.py` produces orders and
refunds by minting a session, opening a checkout, approving the exact version, submitting
it for kernel admission and applying provider capture evidence through the kernel — the
sequence a buyer, an agent and the durable worker perform between them. A seeder that
inserted the rows directly would produce a database that renders correctly and proves
nothing, which would make `/evidence` — a page whose whole purpose is to be checkable — a
decoration. The three places where it stands in for a world a laptop does not have are
named in its docstring and marked `SEEDING SEAM` at the call site.

**`commerce_dev` and `commerce_test` do not mix.** The suites delete only what their own
fixtures created, so demo rows seeded into `commerce_test` would outlive every run and
eventually change what a test sees. `seed_demo_tenant.py` refuses that target outright
rather than warning about it.

See `docs/DEMO.md` for the runbook and `docs/adr/0003-service-layer.md` for why the
processes and roles are split the way they are.
