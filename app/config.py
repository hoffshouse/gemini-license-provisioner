import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    # Google Cloud Project Configuration
    # GCP_PROJECT_ID has no usable default - set it via the GCP_PROJECT_ID env var
    # (the deploy workflow and Terraform do this; export it locally for dev).
    GCP_PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "")
    GCP_REGION: str = os.getenv("GCP_REGION", "us-central1")
    
    # Google Workspace Configuration
    # Placeholder only - set DELEGATED_ADMIN_EMAIL (env var or the Settings page) to a
    # real, active, licensed admin user in your Workspace tenant. An address that does
    # not resolve fails with "invalid_grant: Invalid email or User ID".
    DELEGATED_ADMIN_EMAIL: str = os.getenv("DELEGATED_ADMIN_EMAIL", "workspace-admin@your-domain.com")
    
    # The Gemini Enterprise license subscription to assign from is chosen on the
    # Settings page (a Discovery Engine license config resource name) and stored in
    # Firestore. An optional env override for headless/initial deploys:
    LICENSE_CONFIG: Optional[str] = os.getenv("LICENSE_CONFIG", None)

    # Firestore Configuration
    FIRESTORE_DATABASE: str = os.getenv("FIRESTORE_DATABASE", "(default)")
    CONFIG_COLLECTION: str = "config"
    CONFIG_DOC_ID: str = "gemini_provisioner"
    HISTORY_COLLECTION: str = "sync_history"
    
    # Cloud Scheduler Configuration
    CLOUD_SCHEDULER_JOB_NAME: str = os.getenv("CLOUD_SCHEDULER_JOB_NAME", "gemini-license-sync-job")
    CLOUD_SCHEDULER_LOCATION: str = os.getenv("CLOUD_SCHEDULER_LOCATION", "us-central1")
    
    # Service Account Credentials Override (Optional, used for local testing or Secret Manager)
    SERVICE_ACCOUNT_KEY_PATH: Optional[str] = os.getenv("SERVICE_ACCOUNT_KEY_PATH", None)
    SERVICE_ACCOUNT_KEY_JSON: Optional[str] = os.getenv("SERVICE_ACCOUNT_KEY_JSON", None)

    # Runtime service account email, used for keyless Domain-Wide Delegation on Cloud Run
    # via the IAM Service Account Credentials API. Must be set explicitly because the
    # metadata server commonly reports the attached service account as "default".
    RUNTIME_SERVICE_ACCOUNT_EMAIL: Optional[str] = os.getenv("RUNTIME_SERVICE_ACCOUNT_EMAIL", None)

    # Sync run notifications (email sent via the Gmail API using DWD). The sender
    # mailbox to impersonate; defaults to the delegated admin. The gmail.send scope
    # must be added to the DWD entry for this to work.
    NOTIFICATION_SENDER_EMAIL: Optional[str] = os.getenv("NOTIFICATION_SENDER_EMAIL", None)

    # Public base URL of this service, used to build links in notification emails.
    # Optional: the app also learns it from incoming requests and stores it in config.
    PUBLIC_BASE_URL: Optional[str] = os.getenv("PUBLIC_BASE_URL", None)

    # Web & Security
    PORT: int = int(os.getenv("PORT", 8080))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

    # --- Access control (Identity-Aware Proxy + Workspace super-admin check) ---
    # Enforcement turns ON as soon as IAP_AUDIENCE is set. When set, every page and
    # every mutating API requires a valid IAP assertion whose email is a Google
    # Workspace *super administrator*. Leave empty for local dev / pre-IAP deploys.
    #
    # IAP_AUDIENCE for Cloud Run is "/projects/<PROJECT_NUMBER>/global/backendServices/<ID>"
    # (get it from the IAP console or `gcloud iap ...`; see setup_instructions.md).
    IAP_AUDIENCE: Optional[str] = os.getenv("IAP_AUDIENCE", None)
    IAP_JWT_HEADER: str = os.getenv("IAP_JWT_HEADER", "x-goog-iap-jwt-assertion")

    # Service account the scheduler uses to call POST /api/sync/run. When IAP is on,
    # its IAP assertion email is allowed through even though it is not a super admin.
    SYNC_INVOKER_SA_EMAIL: Optional[str] = os.getenv("SYNC_INVOKER_SA_EMAIL", None)

    # Break-glass allow-list: comma-separated emails always treated as authorized
    # (e.g. if the Directory API is unavailable). Use sparingly.
    AUTH_BOOTSTRAP_ADMINS: str = os.getenv("AUTH_BOOTSTRAP_ADMINS", "")

    # Seconds to cache a "<email> is super admin" lookup.
    SUPER_ADMIN_CACHE_TTL: int = int(os.getenv("SUPER_ADMIN_CACHE_TTL", "300"))

    @property
    def AUTH_ENABLED(self) -> bool:
        return bool(self.IAP_AUDIENCE)

    @property
    def bootstrap_admin_emails(self) -> set:
        return {e.strip().lower() for e in self.AUTH_BOOTSTRAP_ADMINS.split(",") if e.strip()}

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
