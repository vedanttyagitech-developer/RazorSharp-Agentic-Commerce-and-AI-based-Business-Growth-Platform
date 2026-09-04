# Spec 22.1 / 23.2: Cloud SQL for PostgreSQL 16, private IP only, PITR and automated
# backups, IAM database authentication (no passwords in any secret for the app identities).
resource "google_sql_database_instance" "pg" {
  name                = "${local.prefix}-pg-${var.db_instance_suffix}"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.deletion_protection

  settings {
    tier                        = var.db_tier
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    disk_autoresize_limit       = 50
    deletion_protection_enabled = var.deletion_protection
    user_labels                 = local.labels

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.vpc.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "20:00" # 01:30 IST
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    maintenance_window {
      day          = 7
      hour         = 20
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = true
      record_client_address   = false
    }
  }

  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "commerce" {
  name     = "commerce"
  instance = google_sql_database_instance.pg.name
}

# IAM database users: the service account email without ".gserviceaccount.com".
# PostgreSQL role membership (commerce_app etc.) is granted by infra/sql after the first
# migration; the built-in `postgres` user's password is set out of band (docs/DEPLOY.md).
resource "google_sql_user" "iam" {
  for_each = merge(
    { for key, sa in google_service_account.db_identity : key => sa.email },
    { db-migration = google_service_account.workload["db-migration"].email },
  )

  name            = trimsuffix(each.value, ".gserviceaccount.com")
  instance        = google_sql_database_instance.pg.name
  type            = "CLOUD_IAM_SERVICE_ACCOUNT"
  deletion_policy = "ABANDON" # PostgreSQL refuses to drop a user that owns objects or holds roles
}
