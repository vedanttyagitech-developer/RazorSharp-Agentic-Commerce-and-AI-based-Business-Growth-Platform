terraform {
  required_version = ">= 1.6"
  required_providers { google = { source = "hashicorp/google", version = "~> 8.1.0" } }
}
provider "google" {
  project = var.project_id
  region  = var.region
}
variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "asia-south1"
}
variable "image_digest" {
  type    = string
  default = ""
  validation {
    condition     = var.image_digest == "" || can(regex("@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "Deploy an immutable image digest."
  }
}
variable "key_fingerprint" {
  type    = string
  default = ""
}
variable "signing_key_version" {
  type    = number
  default = 1
  validation {
    condition     = var.signing_key_version >= 1 && floor(var.signing_key_version) == var.signing_key_version
    error_message = "An explicit positive immutable key version is required."
  }
}
variable "signing_kid" {
  type    = string
  default = "reserve-hsm-v1"
}
variable "audience" {
  type    = string
  default = "https://unconfigured.invalid"
}
variable "signer_policy" {
  type      = string
  default   = "{\"version\":1,\"expires_at\":0,\"scopes\":[]}"
  sensitive = true
}
resource "google_service_account" "signer" {
  account_id   = "reserve-isolated-signer"
  display_name = "Reserve simulator HSM signer only"
}
resource "google_service_account" "caller" {
  account_id   = "reserve-api-caller"
  display_name = "Dedicated Reserve API caller; no KMS permissions"
}
resource "google_service_account" "builder" {
  account_id   = "reserve-image-builder"
  display_name = "Build isolated signer images; cannot sign authorizations"
}
resource "google_kms_key_ring" "reserve" {
  name     = "reserve-simulator-trust"
  location = var.region
}
resource "google_kms_crypto_key" "authority" {
  name     = "authority-es256"
  key_ring = google_kms_key_ring.reserve.id
  purpose  = "ASYMMETRIC_SIGN"
  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = "HSM"
  }
  lifecycle { prevent_destroy = true }
}
resource "google_kms_crypto_key_iam_member" "signer" {
  crypto_key_id = google_kms_crypto_key.authority.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.signer.email}"
}
resource "google_project_iam_audit_config" "kms" {
  project = var.project_id
  service = "cloudkms.googleapis.com"
  audit_log_config { log_type = "DATA_READ" }
  audit_log_config { log_type = "DATA_WRITE" }
}
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "reserve-signer"
  format        = "DOCKER"
}
resource "google_artifact_registry_repository_iam_member" "builder" {
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}
resource "google_project_iam_member" "builder_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.builder.email}"
}
resource "google_storage_bucket" "source" {
  name                        = "${var.project_id}-reserve-builds"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 7 }
  }
}
resource "google_storage_bucket_iam_member" "builder_source" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.builder.email}"
}
resource "google_cloud_run_v2_service" "signer" {
  count               = var.image_digest == "" ? 0 : 1
  name                = "reserve-isolated-signer"
  location            = var.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  # Cloud Run IAM is mandatory. No allUsers/allAuthenticatedUsers grants.
  template {
    service_account                  = google_service_account.signer.email
    max_instance_request_concurrency = 8
    timeout                          = "30s"
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image_digest
      resources { limits = { cpu = "1", memory = "512Mi" } }
      env {
        name  = "RESERVE_KMS_KEY_VERSION"
        value = "${google_kms_crypto_key.authority.id}/cryptoKeyVersions/${var.signing_key_version}"
      }
      env {
        name  = "RESERVE_KMS_KID"
        value = var.signing_kid
      }
      env {
        name  = "RESERVE_KMS_PUBLIC_SHA256"
        value = var.key_fingerprint
      }
      env {
        name  = "RESERVE_SIGNER_AUDIENCE"
        value = var.audience
      }
      env {
        name  = "RESERVE_SIGNER_POLICY"
        value = var.signer_policy
      }
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.signer]
  lifecycle { prevent_destroy = true }
}
resource "google_cloud_run_v2_service_iam_member" "caller" {
  count    = var.image_digest == "" ? 0 : 1
  location = var.region
  name     = google_cloud_run_v2_service.signer[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.caller.email}"
}
output "key_version" { value = "${google_kms_crypto_key.authority.id}/cryptoKeyVersions/${var.signing_key_version}" }
output "caller_email" { value = google_service_account.caller.email }
output "caller_subject" { value = google_service_account.caller.unique_id }
output "signer_url" { value = try(google_cloud_run_v2_service.signer[0].uri, "") }
