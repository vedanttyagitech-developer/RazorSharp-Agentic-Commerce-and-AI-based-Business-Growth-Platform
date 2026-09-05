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
    durable-worker   = "GKE workload: durable-worker (outbox executor, only Razorpay caller)"
    buyer-web        = "GKE workload: buyer-web (Next.js storefront); no Google API access"
    merchant-console = "GKE workload: merchant-console (Next.js operator console); no Google API access"
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
    { by = "durable-worker", target = "db-commerce-worker" },
    { by = "durable-worker", target = "db-commerce-kernel" },
  ]

  # Secret Manager secrets and the workloads allowed to read each one. Values are added
  # out of band (docs/DEPLOY.md); Terraform never creates a version.
  secrets = {
    razorpay-key-id         = ["commerce-api", "durable-worker"]
    razorpay-key-secret     = ["commerce-api", "durable-worker"]
    razorpay-webhook-secret = ["commerce-api"]
    db-url-app              = ["commerce-api"]
    db-url-kernel           = ["commerce-api", "durable-worker"]
    db-url-worker           = ["durable-worker"]
    gemini-api-key          = ["commerce-api"]

    # The two web surfaces. Each cookie secret is read by exactly one workload, so a
    # storefront pod cannot forge an operator cookie and vice versa.
    web-session-cookie-secret = ["buyer-web"]
    console-cookie-secret     = ["merchant-console"]

    # The scenario key is the sharpest credential in this list: it gates the scenario
    # controller and lets the console mint an OPERATOR session. commerce-api holds it
    # because it is the side that *verifies* it; merchant-console holds it because it is
    # the one surface that presents it. buyer-web is absent from this line on purpose --
    # a storefront that could read this key could mint itself an operator session and read
    # every buyer's orders.
    scenario-key = ["commerce-api", "merchant-console"]
  }
}
