# Secret Manager. File is named sm.tf because the root .gitignore ignores *secret*.
#
# Terraform creates the secrets and their access bindings only. Versions (the values) are
# added with `gcloud secrets versions add` (docs/DEPLOY.md), so no value ever enters
# Terraform state or a plan output.
resource "google_secret_manager_secret" "app" {
  for_each = local.secrets

  secret_id = each.key
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

locals {
  secret_readers = flatten([
    for secret, readers in local.secrets : [
      for reader in readers : { key = "${secret}/${reader}", secret = secret, reader = reader }
    ]
  ])
}

# Per-secret, per-workload accessor. Both principal forms are bound: the GSA (Workload
# Identity annotation on the KSA) and the KSA's own federated identity, which is what the
# Secret Manager add-on documents.
resource "google_secret_manager_secret_iam_member" "gsa" {
  for_each = { for b in local.secret_readers : b.key => b }

  secret_id = google_secret_manager_secret.app[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workload[each.value.reader].email}"
}

resource "google_secret_manager_secret_iam_member" "ksa" {
  for_each = { for b in local.secret_readers : b.key => b }

  secret_id = google_secret_manager_secret.app[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "principal://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.namespace}/sa/${each.value.reader}"
}
