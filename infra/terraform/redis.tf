# Memorystore for Redis 7.2 (spec 22.1, pinned). Spec 22.4: rate limits, ephemeral
# sessions, short caches, presence; never approval, authority, reservation, payment,
# idempotency or audit truth. Basic tier, no replica: losing it degrades, it does not
# decide (spec 23.3). AUTH is off because no Redis credential is in the secret set; the
# instance is reachable only from the VPC and the NetworkPolicy allows only the API.
resource "google_redis_instance" "cache" {
  name                    = "${local.prefix}-redis"
  region                  = var.region
  tier                    = "BASIC"
  memory_size_gb          = var.redis_memory_gb
  redis_version           = "REDIS_7_2"
  authorized_network      = google_compute_network.vpc.id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  reserved_ip_range       = google_compute_global_address.psa.name
  auth_enabled            = false
  transit_encryption_mode = "DISABLED"
  labels                  = local.labels

  redis_configs = {
    maxmemory-policy = "allkeys-lru"
  }

  depends_on = [google_service_networking_connection.psa]
}
