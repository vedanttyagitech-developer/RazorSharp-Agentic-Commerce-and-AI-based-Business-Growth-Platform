-- Login roles for the isolation suite.
--
-- These MUST be NOSUPERUSER and NOBYPASSRLS. PostgreSQL superusers bypass row-level
-- security unconditionally, so an isolation suite run as a superuser passes while
-- proving nothing. conftest.py asserts both flags before any test runs.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_test_app') THEN
    CREATE ROLE commerce_test_app LOGIN PASSWORD 'testpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='commerce_test_kernel') THEN
    CREATE ROLE commerce_test_kernel LOGIN PASSWORD 'testpw' NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

GRANT commerce_app    TO commerce_test_app;
GRANT commerce_kernel TO commerce_test_kernel;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO commerce_app, commerce_kernel;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO commerce_kernel;
GRANT INSERT ON tenants, merchants TO commerce_app;

-- Re-assert the prohibitions after the broad grant above, in this order.
REVOKE INSERT, UPDATE, DELETE ON approvals, delegated_authorities, payment_attempts,
  execution_grants, checkout_versions, policy_at_sale_receipts, reservations,
  idempotency_records FROM commerce_app;
REVOKE UPDATE, DELETE ON audit_events FROM commerce_app, commerce_kernel, commerce_worker;
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM commerce_app, commerce_kernel, commerce_worker;
