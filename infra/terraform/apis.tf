# Spec 22.5: aiplatform + texttospeech plus the GKE / Cloud SQL / Secret Manager APIs.
resource "google_project_service" "required" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "containerscanning.googleapis.com",
    "generativelanguage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "sts.googleapis.com",
    "texttospeech.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}
