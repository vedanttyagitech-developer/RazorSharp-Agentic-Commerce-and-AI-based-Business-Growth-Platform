# Artifact Registry with vulnerability scanning (containerscanning.googleapis.com, apis.tf).
resource "google_artifact_registry_repository" "commerce" {
  location      = var.region
  repository_id = "commerce"
  format        = "DOCKER"
  description   = "commerce-api and action-executor images"
  labels        = local.labels

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-untagged-after-7d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"
    }
  }

  depends_on = [google_project_service.required]
}

# Autopilot nodes pull images with the Compute Engine default service account.
resource "google_artifact_registry_repository_iam_member" "node_pull" {
  location   = google_artifact_registry_repository.commerce.location
  repository = google_artifact_registry_repository.commerce.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
