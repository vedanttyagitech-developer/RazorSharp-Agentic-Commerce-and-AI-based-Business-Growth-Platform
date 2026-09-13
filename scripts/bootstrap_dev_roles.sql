-- Login roles for LOCAL DEVELOPMENT against the commerce_dev database.
--
-- The production roles (commerce_app, commerce_kernel, commerce_worker) are NOLOGIN by
-- design: on GKE they are assumed through Cloud SQL IAM. Locally we need something that
-- can log in, so each dev role is a NOSUPERUSER NOBYPASSRLS login that is a MEMBER of the
-- production role and inherits exactly its grants and nothing more. Passwords here are
-- development-only and this script must never be run against a shared database.
--
-- Run AFTER `alembic upgrade head`. The migrations already carry the exact per-table
-- grants (platform_db.roles.WRITE_GRANTS / FINANCIAL_TABLES). This script creates
-- restricted login memberships and repairs historical evidence over-grants only.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_dev_app') THEN
    CREATE ROLE commerce_dev_app LOGIN PASSWORD 'devpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_dev_kernel') THEN
    CREATE ROLE commerce_dev_kernel LOGIN PASSWORD 'devpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_dev_worker') THEN
    CREATE ROLE commerce_dev_worker LOGIN PASSWORD 'devpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

GRANT commerce_app    TO commerce_dev_app;
GRANT commerce_kernel TO commerce_dev_kernel;
GRANT commerce_worker TO commerce_dev_worker;

-- Table grants belong to migrations and platform_db.rls, never broad bootstrap grants.
-- Also repair the historical bootstrap's accidental evidence/worker permissions.
REVOKE UPDATE ON merchant_policy_versions, verified_authority_proofs, inventory_movements
    FROM commerce_app, commerce_kernel, commerce_worker;
REVOKE INSERT, UPDATE, DELETE ON webhook_inbox FROM commerce_app, commerce_worker;
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM commerce_app, commerce_kernel, commerce_worker;
