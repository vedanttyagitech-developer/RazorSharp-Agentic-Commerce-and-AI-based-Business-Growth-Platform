-- Run ONCE, as the built-in `postgres` user, BEFORE the first migration Job.
-- Where: Cloud SQL Studio (console > SQL > instance > Cloud SQL Studio, user postgres,
-- database commerce) or psql through a local Cloud SQL Auth Proxy. Replace PROJECT_ID.
--
-- The migration identity is the Job's Workload Identity service account, created by
-- Terraform as a Cloud SQL IAM user. The first migration creates the NOLOGIN group roles
-- (platform_db.rls.create_roles_sql), which needs CREATEROLE; on Cloud SQL that comes with
-- membership in cloudsqlsuperuser (not a real superuser, cannot BYPASSRLS).
GRANT cloudsqlsuperuser TO "db-migration@PROJECT_ID.iam";

-- Schema objects will be owned by the migration identity. Application identities get no
-- ownership and no CREATE on the schema.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
