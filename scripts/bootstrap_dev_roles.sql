-- Login roles for LOCAL DEVELOPMENT against the commerce_dev database.
--
-- The production roles (commerce_app, commerce_kernel, commerce_worker) are NOLOGIN by
-- design: on GKE they are assumed through Cloud SQL IAM. Locally we need something that
-- can log in, so each dev role is a NOSUPERUSER NOBYPASSRLS login that is a MEMBER of the
-- production role and inherits exactly its grants and nothing more. Passwords here are
-- development-only and this script must never be run against a shared database.
--
-- Run AFTER `alembic upgrade head`. The migrations already carry the exact per-table
-- grants (platform_db.roles.WRITE_GRANTS / FINANCIAL_TABLES); this script re-asserts
-- them so a database whose migrations predate them still matches.
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
