# ─────────────────────────────────────────────────────────────────────────────────
# Fill these in via terraform.tfvars, -var flags, or TF_VAR_* env vars.
# Nothing here is tied to a specific GCP project or Workspace domain.
# ─────────────────────────────────────────────────────────────────────────────────

variable "project_id" {
  description = "The GCP Project ID where resources will be provisioned. Required."
  type        = string
  # No default on purpose: set it explicitly so nothing environment-specific is baked in.
}

variable "region" {
  description = "GCP region for Cloud Run, Cloud Scheduler, and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the Cloud Run service."
  type        = string
  default     = "gemini-license-provisioner"
}

variable "app_service_account_id" {
  description = "Account ID (the part before '@') of the service account that runs Cloud Run and is impersonated by CI/CD."
  type        = string
  default     = "sa-gemini-provisioner"
}

variable "scheduler_service_account_id" {
  description = "Account ID of the service account Cloud Scheduler uses to invoke the sync endpoint via OIDC."
  type        = string
  default     = "sa-scheduler-invoker"
}

variable "artifact_repository_id" {
  description = "Artifact Registry repository ID that holds the container image."
  type        = string
  default     = "gemini-provisioner-docker"
}

variable "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job that triggers periodic license sync."
  type        = string
  default     = "gemini-license-sync-job"
}

variable "delegated_admin_email" {
  description = "Real, active, licensed Google Workspace admin user to impersonate via Domain-Wide Delegation (e.g. workspace-admin@your-domain.com). Must resolve to an existing user or the sync fails with 'invalid_grant: Invalid email or User ID'."
  type        = string
  default     = "workspace-admin@your-domain.com"
}

variable "enable_iap" {
  description = "Put Identity-Aware Proxy in front of Cloud Run. The app then also requires the IAP user to be a Google Workspace super admin (set iap_audience too)."
  type        = bool
  default     = false
}

variable "iap_audience" {
  description = "Expected 'aud' of the IAP JWT, e.g. /projects/<PROJECT_NUMBER>/global/backendServices/<ID>. Required when enable_iap = true. Get it from the IAP console or `gcloud iap`."
  type        = string
  default     = ""
}

variable "iap_members" {
  description = "IAM members allowed through IAP (roles/iap.httpsResourceAccessor). Empty = ['domain:<domain of delegated_admin_email>']. The app still restricts access to super admins."
  type        = list(string)
  default     = []
}

variable "iap_oauth_client_id" {
  description = "IAP OAuth 2.0 client ID (…apps.googleusercontent.com). Used as the Cloud Scheduler OIDC audience so scheduled sync runs pass through IAP. Required when enable_iap = true for scheduled runs to work."
  type        = string
  default     = ""
}

variable "notification_sender_email" {
  description = "Mailbox to send sync-run notification emails as (Gmail API + DWD). Empty = use delegated_admin_email. Requires the gmail.send scope on the DWD entry."
  type        = string
  default     = ""
}

variable "public_base_url" {
  description = "Public https base URL of the service, used for links in notification emails. Empty = the app learns it from web traffic."
  type        = string
  default     = ""
}

variable "license_config" {
  description = "Optional headless default for the Gemini Enterprise license subscription: a Discovery Engine license config resource name (projects/<NUMBER>/locations/<LOC>/licenseConfigs/<ID>). Normally left empty and chosen on the Settings page."
  type        = string
  default     = ""
}

variable "initial_cron_expression" {
  description = "Initial cron frequency for Cloud Scheduler (UTC)."
  type        = string
  default     = "0 2 * * *"
}

variable "github_repo" {
  description = "GitHub repository in 'owner/repo' format for Workload Identity Federation (e.g. 'my-org/gemini-license-provisioner'). Leave empty to skip WIF resources."
  type        = string
  default     = ""
}

variable "container_image" {
  description = "Docker container image URI for Cloud Run. Overridden by CI/CD on each deploy."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}
