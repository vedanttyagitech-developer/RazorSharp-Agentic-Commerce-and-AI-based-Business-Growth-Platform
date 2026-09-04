# Spec 22.2: GKE Autopilot as the unified runtime. Autopilot enables Workload Identity
# Federation, Dataplane V2 (NetworkPolicy enforcement), Shielded nodes and node
# auto-upgrade by itself; the settings below are the ones that are choices.
resource "google_container_cluster" "autopilot" {
  name     = local.prefix
  location = var.region

  enable_autopilot    = true
  network             = google_compute_network.vpc.id
  subnetwork          = google_compute_subnetwork.gke.id
  deletion_protection = var.deletion_protection
  resource_labels     = local.labels

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Private nodes (no public IPs); public control-plane endpoint guarded by IAM and,
  # when set, master_authorized_cidrs.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_cidrs) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_cidrs
        content {
          display_name = cidr_blocks.value.name
          cidr_block   = cidr_blocks.value.cidr
        }
      }
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Secret Manager add-on: SecretProviderClass provider `gke`, driver secrets-store-gke.csi.k8s.io.
  secret_manager_config {
    enabled = true
  }

  # FQDNNetworkPolicy (api.razorpay.com, *.googleapis.com allowlists). Update-only on Autopilot.
  enable_fqdn_network_policy = var.enable_fqdn_network_policy

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = true
    }
  }

  maintenance_policy {
    recurring_window {
      start_time = "2026-01-03T20:00:00Z" # 01:30 IST
      end_time   = "2026-01-04T00:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
    }
  }

  depends_on = [
    google_project_service.required,
    google_service_networking_connection.psa,
  ]
}
