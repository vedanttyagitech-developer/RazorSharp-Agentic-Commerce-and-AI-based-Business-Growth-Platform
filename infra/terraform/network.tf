resource "google_compute_network" "vpc" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "gke" {
  name                     = "${local.prefix}-gke"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = "10.10.0.0/20"
  private_ip_google_access = true # googleapis.com from private nodes without NAT

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.11.0.0/16"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.12.0.0/20"
  }

  log_config {
    aggregation_interval = "INTERVAL_5_MIN"
    flow_sampling        = 0.1
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Private services access: Cloud SQL and Memorystore are allocated from this range.
resource "google_compute_global_address" "psa" {
  name          = "${local.prefix}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = local.psa_address
  prefix_length = local.psa_prefix_length
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]
  deletion_policy         = "ABANDON"
}

# Private nodes reach api.razorpay.com through Cloud NAT; Google APIs use Private Google Access.
resource "google_compute_router" "nat" {
  name    = "${local.prefix}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "${local.prefix}-nat"
  router                             = google_compute_router.nat.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Global external Application Load Balancer front end (GKE Ingress).
resource "google_compute_global_address" "ingress" {
  name         = "${local.prefix}-ingress"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"

  depends_on = [google_project_service.required]
}

# Referenced by the FrontendConfig (spec 21.9: encrypt in transit, TLS 1.2+).
resource "google_compute_ssl_policy" "modern" {
  name            = "${local.prefix}-tls-modern"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"

  depends_on = [google_project_service.required]
}
