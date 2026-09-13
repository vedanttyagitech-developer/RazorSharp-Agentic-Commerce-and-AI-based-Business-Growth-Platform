-- Login roles for the isolation suite.
--
-- These MUST be NOSUPERUSER and NOBYPASSRLS. PostgreSQL superusers bypass row-level
-- security unconditionally, so an isolation suite run as a superuser passes while
-- proving nothing. conftest.py asserts both flags before any test runs.
--
-- Run AFTER `alembic upgrade head`. The migrations already carry the exact per-table
-- grants (platform_db.roles.WRITE_GRANTS / FINANCIAL_TABLES); this script only creates
-- the login members and repairs historical evidence over-grants. Run migrations
-- first to install the complete current privilege matrix.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_test_app') THEN
    CREATE ROLE commerce_test_app LOGIN PASSWORD 'testpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_test_kernel') THEN
    CREATE ROLE commerce_test_kernel LOGIN PASSWORD 'testpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_test_worker') THEN
    CREATE ROLE commerce_test_worker LOGIN PASSWORD 'testpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

GRANT commerce_app    TO commerce_test_app;
GRANT commerce_kernel TO commerce_test_kernel;
GRANT commerce_worker TO commerce_test_worker;

-- Table grants belong to migrations and platform_db.rls, never broad bootstrap grants.
-- Also repair the historical bootstrap's accidental evidence/worker permissions.
REVOKE UPDATE ON merchant_policy_versions, verified_authority_proofs, inventory_movements
    FROM commerce_app, commerce_kernel, commerce_worker;
REVOKE INSERT, UPDATE, DELETE ON webhook_inbox FROM commerce_app, commerce_worker;
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM commerce_app, commerce_kernel, commerce_worker;
