-- Login roles for the isolation suite.
--
-- These MUST be NOSUPERUSER and NOBYPASSRLS. PostgreSQL superusers bypass row-level
-- security unconditionally, so an isolation suite run as a superuser passes while
-- proving nothing. conftest.py asserts both flags before any test runs.
--
-- Run AFTER `alembic upgrade head`. The migrations already carry the exact per-table
-- grants (platform_db.roles.WRITE_GRANTS / FINANCIAL_TABLES); this script only creates
-- the login members and re-asserts the grants so a database whose migrations predate
-- them still matches. The prohibitions at the bottom are what the tests prove.
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

GRANT SELECT ON ALL TABLES IN SCHEMA public TO commerce_app, commerce_kernel, commerce_worker;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO commerce_kernel;
GRANT INSERT ON tenants, merchants TO commerce_app;

-- Service tables (ADR 0003). Mirrors platform_db.roles.WRITE_GRANTS exactly.
GRANT INSERT, UPDATE ON api_sessions, carts, checkouts, scenario_runs TO commerce_app;
GRANT INSERT ON scenario_faults TO commerce_app;
GRANT UPDATE ON outbox_events, webhook_inbox, scenario_faults TO commerce_worker;
GRANT INSERT ON audit_events TO commerce_worker;

-- Re-assert the prohibitions after the broad grants above, in this order.
REVOKE INSERT, UPDATE, DELETE ON approvals, delegated_authorities, payment_attempts, refunds,
  execution_grants, checkout_versions, policy_at_sale_receipts, reservations,
  idempotency_records, orders, provider_requests, reconciliation_runs
  FROM commerce_app, commerce_worker;
REVOKE INSERT, UPDATE, DELETE ON webhook_inbox FROM commerce_app;
REVOKE INSERT ON scenario_faults FROM commerce_kernel;
REVOKE UPDATE, DELETE ON audit_events FROM commerce_app, commerce_kernel, commerce_worker;
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM commerce_app, commerce_kernel, commerce_worker;
