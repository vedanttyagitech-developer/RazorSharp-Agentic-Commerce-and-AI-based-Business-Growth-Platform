-- Run ONCE, as `postgres` (or as the migration identity), AFTER the first successful
-- migration Job: the group roles exist only once `alembic upgrade head` has run.
-- Replace PROJECT_ID. Re-running is harmless.
--
-- ADR 0003 D1: the API connects as two identities (app for reads, kernel for mutations),
-- the worker as two (worker for the outbox, kernel for kernel calls). Each Cloud SQL Auth
-- Proxy sidecar impersonates exactly one of these service accounts, and each service
-- account is a member of exactly one PostgreSQL group role. The migrations carry the
-- per-table grants; nothing here widens them.
GRANT commerce_app    TO "db-commerce-app@PROJECT_ID.iam";
GRANT commerce_kernel TO "db-commerce-kernel@PROJECT_ID.iam";
GRANT commerce_worker TO "db-commerce-worker@PROJECT_ID.iam";

-- Sanity: every application identity must be NOSUPERUSER NOBYPASSRLS (spec 21.3). Cloud
-- SQL creates IAM users that way; this query must return three rows with both false.
SELECT rolname, rolsuper, rolbypassrls, rolcreaterole
FROM pg_roles
WHERE rolname IN ('db-commerce-app@PROJECT_ID.iam',
                  'db-commerce-kernel@PROJECT_ID.iam',
                  'db-commerce-worker@PROJECT_ID.iam');
