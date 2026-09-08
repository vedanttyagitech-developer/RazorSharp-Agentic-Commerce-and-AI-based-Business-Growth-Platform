variable "project_id" {
  description = "GCP project id. One project per environment: service-account ids are project-unique."
  type        = string
}

variable "region" {
  description = "Region for every regional resource (spec 21.9: one region, region-aware design)."
  type        = string
  default     = "asia-south1"
}

variable "env" {
  description = "Environment name; prefixes resource names (commerce-<env>) and matches infra/kubernetes/overlays/<env>."
  type        = string
  default     = "demo"
  validation {
    condition     = contains(["dev", "demo"], var.env)
    error_message = "env must be dev or demo (the overlays that exist)."
  }
}

variable "namespace" {
  description = "Kubernetes namespace the workloads run in (Workload Identity bindings reference it)."
  type        = string
  default     = "commerce"
}

variable "deletion_protection" {
  description = "Deletion protection on the GKE cluster and the Cloud SQL instance. Set to false and apply before `terraform destroy`."
  type        = bool
  default     = true
}

variable "enable_fqdn_network_policy" {
  description = "FQDNNetworkPolicy support. Autopilot only accepts it as an update, so apply once with false (cluster creation), then set true and apply again."
  type        = bool
  default     = false
}

variable "master_authorized_cidrs" {
  description = "Source CIDRs allowed to reach the (public) control-plane endpoint. Empty keeps it open to any IP; IAM still authenticates. Set your own CIDR for the demo."
  type = list(object({
    name = string
    cidr = string
  }))
  default = []
}

variable "db_tier" {
  description = "Cloud SQL machine tier. db-custom-1-3840 (1 vCPU, 3.75 GB) is the smallest dedicated-core tier and supports PITR."
  type        = string
  default     = "db-custom-1-3840"
}

variable "db_instance_suffix" {
  description = "Cloud SQL instance names cannot be reused for about a week after deletion; bump this when recreating. Must match INSTANCE_CONNECTION_NAME in the Kubernetes overlay."
  type        = string
  default     = "01"
}

variable "grant_vertex_ai" {
  description = "Grant roles/aiplatform.user to the API service account (Gemini through Vertex AI / ADC). Not needed when the Gemini Developer API key is used."
  type        = bool
  default     = true
}
