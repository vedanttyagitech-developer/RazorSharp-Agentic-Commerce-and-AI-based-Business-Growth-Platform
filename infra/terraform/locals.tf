locals {
  prefix = "commerce-${var.env}"

  labels = {
    app        = "governed-agentic-commerce"
    env        = var.env
    managed-by = "terraform"
  }

  # Private services access range for Cloud SQL and Memorystore. Mirrored in
  # infra/kubernetes/base/workloads/networkpolicies.yaml (egress allow); change both.
  psa_address       = "10.20.0.0"
  psa_prefix_length = 16

  # Workload service accounts, one per Kubernetes ServiceAccount of the same name.
  workloads = {
    commerce-api     = "GKE workload: commerce-api (FastAPI)"
    action-executor  = "GKE workload: action-executor (outbox executor, only Razorpay caller)"
    db-migration     = "GKE Job: alembic upgrade head; Cloud SQL IAM user, cloudsqlsuperuser"
  }

  # Database identities: one IAM database user per PostgreSQL group role (ADR 0003 D1).
  # The proxies impersonate these; infra/sql/02-grant-app-identities.sql grants the role.
  db_identities = {
    db-commerce-app    = "commerce_app"
    db-commerce-kernel = "commerce_kernel"
    db-commerce-worker = "commerce_worker"
  }

  # Which workload may mint tokens for which database identity.
  impersonations = [
    { by = "commerce-api", target = "db-commerce-app" },
    { by = "commerce-api", target = "db-commerce-kernel" },
    { by = "action-executor", target = "db-commerce-worker" },
    { by = "action-executor", target = "db-commerce-kernel" },
  ]

  # Secret Manager secrets and the workloads allowed to read each one. Values are added
  # out of band (docs/DEPLOY.md); Terraform never creates a version.
  secrets = {
    razorpay-key-id         = ["commerce-api", "action-executor"]
    razorpay-key-secret     = ["commerce-api", "action-executor"]
    razorpay-webhook-secret = ["commerce-api"]
    db-url-app              = ["commerce-api"]
    db-url-kernel           = ["commerce-api", "action-executor"]
    db-url-worker           = ["action-executor"]
    gemini-api-key          = ["commerce-api"]

    # The scenario key gates the scenario controller and mints OPERATOR sessions.
    # commerce-api holds it because it is the side that *verifies* it.
    #
    # The two web cookie secrets left with the front end on 2026-09-09, and so did the
    # console's grant on this key. The rule they encoded is worth keeping for whatever
    # presents it next: each cookie secret was read by exactly one workload so a
    # storefront pod could not forge an operator cookie, and the storefront was absent
    # from this line on purpose -- a storefront that could read this key could mint itself
    # an operator session and read every buyer's orders.
    scenario-key = ["commerce-api"]
  }
}
