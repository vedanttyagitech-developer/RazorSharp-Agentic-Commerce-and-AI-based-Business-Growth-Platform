output "cluster_name" {
  value = google_container_cluster.autopilot.name
}

output "cluster_location" {
  value = google_container_cluster.autopilot.location
}

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.autopilot.name} --region ${var.region} --project ${var.project_id}"
}

output "instance_connection_name" {
  description = "INSTANCE_CONNECTION_NAME for the Kubernetes overlay ConfigMap."
  value       = google_sql_database_instance.pg.connection_name
}

output "db_private_ip" {
  value = google_sql_database_instance.pg.private_ip_address
}

output "db_iam_users" {
  description = "IAM database user names; used in the db-url-* secret values and infra/sql."
  value       = { for key, user in google_sql_user.iam : key => user.name }
}

output "redis_host" {
  description = "REDIS_HOST for the Kubernetes overlay ConfigMap (REDIS_URL=redis://<host>:6379/0)."
  value       = google_redis_instance.cache.host
}

output "redis_port" {
  value = google_redis_instance.cache.port
}

output "artifact_registry" {
  description = "Image prefix: <registry>/commerce-api:<tag> etc."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.commerce.repository_id}"
}

output "ingress_static_ip_name" {
  description = "kubernetes.io/ingress.global-static-ip-name annotation value."
  value       = google_compute_global_address.ingress.name
}

output "ingress_ip" {
  description = "Create the DNS A record for the demo host pointing here."
  value       = google_compute_global_address.ingress.address
}

output "ssl_policy_name" {
  description = "FrontendConfig spec.sslPolicy."
  value       = google_compute_ssl_policy.modern.name
}

output "workload_service_accounts" {
  value = { for key, sa in google_service_account.workload : key => sa.email }
}

output "db_identity_service_accounts" {
  description = "DB_SA_APP / DB_SA_KERNEL / DB_SA_WORKER for the overlay ConfigMap."
  value       = { for key, sa in google_service_account.db_identity : key => sa.email }
}

output "secret_names" {
  value = sort(keys(google_secret_manager_secret.app))
}

output "psa_cidr" {
  description = "Private services access range; mirrored in the NetworkPolicies."
  value       = "${google_compute_global_address.psa.address}/${google_compute_global_address.psa.prefix_length}"
}
