terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.20.0" # iap_enabled on google_cloud_run_v2_service
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {}

locals {
  # Workspace domain inferred from the delegated admin address; used for the
  # default IAP allow-list. Override with var.iap_members.
  workspace_domain = try(split("@", var.delegated_admin_email)[1], "example.com")
  iap_members      = length(var.iap_members) > 0 ? var.iap_members : ["domain:${local.workspace_domain}"]
  iap_sa_member    = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

# -----------------------------------------------------------------------------
# 1. Enable Required GCP APIs
# -----------------------------------------------------------------------------
locals {
  services = [
    "admin.googleapis.com",            # Admin SDK Directory (group / user reads via DWD)
    "discoveryengine.googleapis.com",  # Gemini Enterprise license configs + user licenses
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudbuild.googleapis.com",
    "gmail.googleapis.com",            # only used for run-notification emails
  ]
}

resource "google_project_service" "apis" {
  for_each                   = toset(locals.services)
  project                    = var.project_id
  service                    = each.key
  disable_on_destroy         = false
  disable_dependent_services = false
}

# -----------------------------------------------------------------------------
# 2. Artifact Registry Repository
# -----------------------------------------------------------------------------
resource "google_artifact_registry_repository" "docker_repo" {
  depends_on    = [google_project_service.apis]
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Docker repository for Gemini Enterprise license provisioner"
  format        = "DOCKER"
}

# -----------------------------------------------------------------------------
# 3. Service Accounts & IAM Permissions
# -----------------------------------------------------------------------------

# Application Service Account (Cloud Run)
resource "google_service_account" "app_sa" {
  account_id   = var.app_service_account_id
  display_name = "Gemini License Provisioner Application Service Account"
  description  = "Runs Cloud Run service, manages Firestore config, and calls Workspace APIs via DWD"
}

# Grant Firestore (Datastore User) access
resource "google_project_iam_member" "sa_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# Grant Cloud Scheduler Admin (to modify schedule trigger via API)
resource "google_project_iam_member" "sa_scheduler_admin" {
  project = var.project_id
  role    = "roles/cloudscheduler.admin"
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# Grant Cloud Logging Writer
resource "google_project_iam_member" "sa_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# Manage Gemini Enterprise licenses (list license configs, list/assign user
# licenses via the Discovery Engine API). This is called with the service
# account's own credentials - not Domain-Wide Delegation.
resource "google_project_iam_member" "sa_gemini_licenses" {
  project = var.project_id
  role    = "roles/discoveryengine.admin"
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# Allow the application service account to mint signed JWTs as itself (IAM Credentials
# API: signBlob). This is what enables keyless Google Workspace Domain-Wide Delegation
# from Cloud Run - the app signs a JWT asserting the delegated-admin subject and
# exchanges it for an access token, with no exported service account key.
resource "google_service_account_iam_member" "sa_token_creator_self" {
  service_account_id = google_service_account.app_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.app_sa.email}"
}

# Cloud Scheduler Invoker Service Account
resource "google_service_account" "scheduler_sa" {
  account_id   = var.scheduler_service_account_id
  display_name = "Cloud Scheduler Invoker Service Account"
  description  = "Used by Cloud Scheduler to invoke the /api/sync/run endpoint with OIDC auth"
}

# -----------------------------------------------------------------------------
# 4. Cloud Run Service
# -----------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "provisioner" {
  depends_on = [
    google_project_service.apis,
    google_project_iam_member.sa_firestore,
    google_service_account_iam_member.sa_token_creator_self
  ]
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Identity-Aware Proxy. When true the app also requires the IAP-asserted user
  # to be a Google Workspace super admin (needs IAP_AUDIENCE env var, below).
  iap_enabled = var.enable_iap

  lifecycle {
    precondition {
      condition     = !var.enable_iap || var.iap_audience != ""
      error_message = "Set var.iap_audience when enable_iap = true, so the app can verify the IAP JWT and enforce the Workspace super-admin check."
    }
  }

  template {
    service_account = google_service_account.app_sa.email
    timeout         = "600s" # 10-minute timeout for large directory sync runs

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1000m"
          memory = "1024Mi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "DELEGATED_ADMIN_EMAIL"
        value = var.delegated_admin_email
      }
      env {
        name  = "RUNTIME_SERVICE_ACCOUNT_EMAIL"
        value = google_service_account.app_sa.email
      }
      env {
        name  = "IAP_AUDIENCE"
        value = var.iap_audience
      }
      env {
        name  = "SYNC_INVOKER_SA_EMAIL"
        value = google_service_account.scheduler_sa.email
      }
      env {
        name  = "NOTIFICATION_SENDER_EMAIL"
        value = var.notification_sender_email
      }
      env {
        name  = "PUBLIC_BASE_URL"
        value = var.public_base_url
      }
      # The Gemini Enterprise license subscription is chosen on the Settings page
      # (stored in Firestore). Optional headless override:
      env {
        name  = "LICENSE_CONFIG"
        value = var.license_config
      }
      env {
        name  = "CLOUD_SCHEDULER_JOB_NAME"
        value = var.scheduler_job_name
      }
      env {
        name  = "CLOUD_SCHEDULER_LOCATION"
        value = var.region
      }

      ports {
        container_port = 8080
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 10
        failure_threshold     = 3
      }
    }
  }
}

# Allow Scheduler SA to invoke Cloud Run
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  location = google_cloud_run_v2_service.provisioner.location
  name     = google_cloud_run_v2_service.provisioner.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

# Public invoker binding - ONLY when IAP is disabled. With enable_iap = true this
# is dropped and the IAP service agent (below) is the sole run.invoker.
# WARNING (enable_iap = false): the admin UI and every /api/* endpoint are then
# public and unauthenticated. See "Security Model" in setup_instructions.md.
resource "google_cloud_run_v2_service_iam_member" "public_access" {
  count    = var.enable_iap ? 0 : 1
  location = google_cloud_run_v2_service.provisioner.location
  name     = google_cloud_run_v2_service.provisioner.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# -----------------------------------------------------------------------------
# 4b. Identity-Aware Proxy (only when enable_iap = true)
# -----------------------------------------------------------------------------
# The IAP service agent invokes Cloud Run on the authenticated user's behalf.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  count    = var.enable_iap ? 1 : 0
  location = google_cloud_run_v2_service.provisioner.location
  name     = google_cloud_run_v2_service.provisioner.name
  role     = "roles/run.invoker"
  member   = local.iap_sa_member
}

# Who may pass through IAP. The app further restricts this to Workspace super
# admins, so a domain-wide grant here is acceptable; tighten to a group if you
# prefer. Set var.iap_members to override the default (domain:<workspace_domain>).
resource "google_iap_web_cloud_run_service_iam_member" "accessors" {
  for_each              = toset(var.enable_iap ? local.iap_members : [])
  project               = var.project_id
  location              = var.region
  cloud_run_service_name = google_cloud_run_v2_service.provisioner.name
  role                  = "roles/iap.httpsResourceAccessor"
  member                = each.value
}

# Cloud Scheduler's SA must also be allowed through IAP to reach /api/sync/run.
resource "google_iap_web_cloud_run_service_iam_member" "scheduler_accessor" {
  count                 = var.enable_iap ? 1 : 0
  project               = var.project_id
  location              = var.region
  cloud_run_service_name = google_cloud_run_v2_service.provisioner.name
  role                  = "roles/iap.httpsResourceAccessor"
  member                = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

# -----------------------------------------------------------------------------
# 5. Cloud Scheduler Job
# -----------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "sync_job" {
  depends_on  = [google_project_service.apis, google_cloud_run_v2_service.provisioner]
  name        = var.scheduler_job_name
  description = "Triggers Google Workspace Gemini license provisioning based on Google Groups"
  schedule    = var.initial_cron_expression
  time_zone   = "UTC"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.provisioner.uri}/api/sync/run"

    headers = {
      "Content-Type" = "application/json"
    }

    body = base64encode(jsonencode({
      "triggered_by" = "scheduled"
    }))

    # With IAP on, the OIDC token must be minted for the IAP OAuth client ID so
    # IAP accepts it; otherwise it targets the Cloud Run URL directly.
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = var.enable_iap && var.iap_oauth_client_id != "" ? var.iap_oauth_client_id : google_cloud_run_v2_service.provisioner.uri
    }
  }
}

# -----------------------------------------------------------------------------
# 6. Workload Identity Federation (GitHub Actions CI/CD)
# -----------------------------------------------------------------------------
resource "google_iam_workload_identity_pool" "github_pool" {
  count                     = var.github_repo != "" ? 1 : 0
  depends_on                = [google_project_service.apis]
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions Pool"
  description               = "Workload Identity Pool for GitHub Actions automated deployments"
}

resource "google_iam_workload_identity_pool_provider" "github_provider" {
  count                              = var.github_repo != "" ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_pool[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub Provider"
  
  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.actor"            = "assertion.actor"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
  }

  attribute_condition = "assertion.repository == '${var.github_repo}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Allow GitHub Actions to impersonate Application SA for deployment
resource "google_service_account_iam_member" "github_sa_user" {
  count              = var.github_repo != "" ? 1 : 0
  service_account_id = google_service_account.app_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_pool[0].name}/attribute.repository/${var.github_repo}"
}
