-- Login roles for LOCAL DEVELOPMENT against the commerce_dev database.
--
-- The production roles (commerce_app, commerce_kernel, commerce_worker) are NOLOGIN by
-- design: on GKE they are assumed through Cloud SQL IAM. Locally we need something that
-- can log in, so each dev role is a NOSUPERUSER NOBYPASSRLS login that is a MEMBER of the
-- production role and inherits exactly its grants and nothing more. Passwords here are
-- development-only and this script must never be run against a shared database.
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

GRANT SELECT ON ALL TABLES IN SCHEMA public TO commerce_app, commerce_kernel, commerce_worker;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO commerce_kernel;
GRANT INSERT ON tenants, merchants TO commerce_app;

-- Re-assert the prohibitions after the broad grants above, in this order.
REVOKE INSERT, UPDATE, DELETE ON approvals, delegated_authorities, payment_attempts,
  execution_grants, checkout_versions, policy_at_sale_receipts, reservations,
  idempotency_records FROM commerce_app, commerce_worker;
REVOKE UPDATE, DELETE ON audit_events FROM commerce_app, commerce_kernel, commerce_worker;
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM commerce_app, commerce_kernel, commerce_worker;
