terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # Remote state: create the bucket once, then uncomment.
  #   gcloud storage buckets create gs://PROJECT_ID-tfstate --location=asia-south1 \
  #     --uniform-bucket-level-access --public-access-prevention
  # backend "gcs" {
  #   bucket = "PROJECT_ID-tfstate"
  #   prefix = "commerce/demo"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {}
