# scripts

Six files. Three of them create database state, and it matters which database each one
touches, so that is the first column.

| Script | Database | What it is for |
| --- | --- | --- |
| `bootstrap_local.sh` | `commerce_dev` **and** `commerce_test` | One command from a clean machine to a working local setup. Creates both databases if absent, applies the migrations to each, installs the restricted login roles, and then verifies that every one of them is `NOSUPERUSER` and `NOBYPASSRLS` and can actually connect. Idempotent and non-destructive. |
| `seed_demo_tenant.py` | `commerce_dev` only | The one tenant and one merchant the demonstration needs. Refuses to run against `commerce_test`. Idempotent: re-running writes nothing and prints the same identifiers. |
| `seed_demo_state.py` | `commerce_dev` only | The orders, refunds, dead letters and refusals `seed_demo_tenant.py` deliberately does not create. Drives the real paths against the running API and worker rather than inserting rows: an order exists only where verified capture evidence put it there. Convergent, so re-running writes nothing; `--reset` rebuilds from empty. See `docs/DEMO.md` section 2. |
| `run_demo.sh` | reads `.env`, targets `commerce_dev`; any database via `DEMO_DB` | Starts the API and the durable worker together and stops both cleanly on Ctrl-C. `--api-only` and `--worker-only` are what `make api` and `make worker` call. `commerce_dev` and `commerce_test` need nothing but `DEMO_DB`; any other database runs on the three `DATABASE_URL_APP` / `_KERNEL` / `_WORKER` URLs you export, which win over `.env`. That plus a free `PORT` is a stack of your own to inject failures into — see `scripts/run_demo.sh --help`. |
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

## Running a suite against a database that is not `commerce_test`

The default is `localhost:5432/commerce_test`, hardcoded at
`packages/platform-db/tests/conftest.py` and `packages/transaction-kernel/tests/conftest.py`.
Two pytest runs pointed at one database fight over rows and locks, and the contention tests
in `test_grants.py` and `test_admission.py` open real contending sessions and assert a single
winner — so they fail nondeterministically rather than cleanly. Any second checkout of this
repository, and CI, therefore needs its own database.

One database per checkout, same cluster, same roles. `bootstrap_test_roles.sql` is idempotent
and its `GRANT`s apply to whichever database is connected, so `setup_ci_db.py` is all it takes:

```bash
createdb commerce_test_x
PATH="$HOME/.local/bin:$PATH" \
  DATABASE_URL=postgresql+psycopg://$USER@localhost:5432/commerce_test_x \
  DATABASE_URL_TEST_ADMIN=postgresql+psycopg://$USER@localhost:5432/commerce_test_x \
  uv run --no-sync python scripts/setup_ci_db.py
```

Both of those variables are needed: one is what the suites connect with, the other is what
creates and grants. Then export all five names for the run — the four the suites read plus
the base URL — at that database:

| Variable | Read by |
| --- | --- |
| `DATABASE_URL` | the API, the worker, and anything that does not ask for a specific role |
| `DATABASE_URL_TEST_ADMIN` | migrations and role bootstrap; the only one that may create |
| `DATABASE_URL_TEST_KERNEL` | the tenant-isolation and kernel suites, as `commerce_test_kernel` |
| `DATABASE_URL_TEST_APP` | the app-role reads, as `commerce_test_app` |
| `DATABASE_URL_TEST_WORKER` | the durable-worker suites and the role-privilege suites, as `commerce_test_worker` |

`.github/workflows/ci.yml` sets exactly these five. A checkout that exports none of them does
not fail — it quietly runs against `commerce_test` and contends with whatever else is using
it, which is the one silent failure mode here and the reason the list is written down.

A database cloned from the CI baseline still needs `alembic upgrade head` from
`packages/platform-db` before the suites will pass; the roles come from `setup_ci_db.py`, the
schema does not.
