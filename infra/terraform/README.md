# Terraform (GCP, single region)

| File | Contents |
| --- | --- |
| `versions.tf` | provider pins (`google ~> 8.0`), optional GCS backend |
| `variables.tf`, `locals.tf` | inputs; naming (`commerce-<env>`), identities, secret list |
| `apis.tf` | required services |
| `network.tf` | VPC, subnet (+ pods/services ranges), private services access, Cloud NAT, ingress IP, SSL policy |
| `gke.tf` | GKE Autopilot: private nodes, Workload Identity, REGULAR channel, Secret Manager add-on, logging/monitoring, FQDN policy flag |
| `cloudsql.tf` | Cloud SQL PostgreSQL 16, private IP, PITR, IAM auth, database `commerce`, IAM users |
| `redis.tf` | Memorystore Redis 7.2 Basic (non-authoritative) |
| `registry.tf` | Artifact Registry + node pull binding |
| `iam.tf` | service accounts, Workload Identity, impersonation, least-privilege roles |
| `sm.tf` | Secret Manager secrets and per-secret accessor bindings (no versions) |
| `outputs.tf` | values the Kubernetes overlay and docs/DEPLOY.md need |

Two applies: the first creates everything with `enable_fqdn_network_policy = false`
(Autopilot accepts the flag only as an update); the second, with `true`, turns the FQDN
allowlists on. Full procedure in `docs/DEPLOY.md`.
