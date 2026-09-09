# Spec 21.8 / 22.5: Workload Identity Federation for GKE, no key files, least privilege.

resource "google_service_account" "workload" {
  for_each     = local.workloads
  account_id   = each.key
  display_name = each.value

  depends_on = [google_project_service.required]
}

resource "google_service_account" "db_identity" {
  for_each     = local.db_identities
  account_id   = each.key
  display_name = "Cloud SQL IAM identity for PostgreSQL role ${each.value}"

  depends_on = [google_project_service.required]
}

# KSA <namespace>/<name> may act as GSA <name>.
resource "google_service_account_iam_member" "workload_identity" {
  for_each           = local.workloads
  service_account_id = google_service_account.workload[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${each.key}]"
}

# Cloud SQL Auth Proxy sidecars: --impersonate-service-account=<db identity>.
resource "google_service_account_iam_member" "impersonate" {
  for_each = { for p in local.impersonations : "${p.by}->${p.target}" => p }

  service_account_id = google_service_account.db_identity[each.value.target].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.workload[each.value.by].email}"
}

locals {
  cloudsql_principals = merge(
    { for key, sa in google_service_account.db_identity : key => sa.email },
    { db-migration = google_service_account.workload["db-migration"].email },
  )
  cloudsql_roles  = ["roles/cloudsql.client", "roles/cloudsql.instanceUser"]
  telemetry_roles = ["roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/cloudtrace.agent"]
}

# Identities that open Cloud SQL connections (through the proxy) and log in with IAM.
resource "google_project_iam_member" "cloudsql" {
  for_each = {
    for pair in setproduct(keys(local.cloudsql_principals), local.cloudsql_roles) :
    "${pair[0]}/${pair[1]}" => { principal = pair[0], role = pair[1] }
  }

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${local.cloudsql_principals[each.value.principal]}"
}

# OpenTelemetry exporters (spec 22.1) for the API and the worker.
resource "google_project_iam_member" "telemetry" {
  for_each = {
    for pair in setproduct(["commerce-api", "action-executor"], local.telemetry_roles) :
    "${pair[0]}/${pair[1]}" => { workload = pair[0], role = pair[1] }
  }

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.workload[each.value.workload].email}"
}

# Gemini through Vertex AI (ADC). Cloud Text-to-Speech needs only the API enabled.
resource "google_project_iam_member" "vertex_ai" {
  count = var.grant_vertex_ai ? 1 : 0

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.workload["commerce-api"].email}"
}
