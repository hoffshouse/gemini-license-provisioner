import json
import logging
import os
from typing import Dict, Any, List, Optional

import google.auth
from google.auth import iam
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.config import settings

logger = logging.getLogger("gemini_provisioner.workspace")

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]

# Requested only when sending run notifications. Must be added to the service
# account's Domain-Wide Delegation entry separately (see setup_instructions.md);
# it is intentionally not in SCOPES so the directory/licensing calls keep working
# even if this scope has not been authorized.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"


class WorkspaceClient:
    """Client for interacting with Google Workspace Admin Directory and Licensing APIs."""

    def __init__(self, delegated_admin_email: Optional[str] = None):
        self.delegated_admin_email = delegated_admin_email or settings.DELEGATED_ADMIN_EMAIL

    def get_credentials(self, subject_email: Optional[str] = None,
                        scopes: Optional[List[str]] = None):
        """Build Google OAuth2 credentials with Domain-Wide Delegation (DWD).

        `scopes` defaults to the directory/licensing SCOPES; pass a narrower list
        (e.g. [GMAIL_SEND_SCOPE]) for other APIs.
        """
        subject = subject_email or self.delegated_admin_email
        scopes = scopes or SCOPES

        # 1. Check if raw JSON string is provided in env var (e.g. from Secret Manager)
        if settings.SERVICE_ACCOUNT_KEY_JSON:
            try:
                key_info = json.loads(settings.SERVICE_ACCOUNT_KEY_JSON)
                creds = service_account.Credentials.from_service_account_info(
                    key_info, scopes=scopes
                )
                return creds.with_subject(subject) if subject else creds
            except Exception as e:
                logger.error("Failed to parse SERVICE_ACCOUNT_KEY_JSON: %s", e)
                raise

        # 2. Check if file path is provided in env var
        if settings.SERVICE_ACCOUNT_KEY_PATH and os.path.exists(settings.SERVICE_ACCOUNT_KEY_PATH):
            try:
                creds = service_account.Credentials.from_service_account_file(
                    settings.SERVICE_ACCOUNT_KEY_PATH, scopes=scopes
                )
                return creds.with_subject(subject) if subject else creds
            except Exception as e:
                logger.error("Failed to load SERVICE_ACCOUNT_KEY_PATH: %s", e)
                raise

        # 3. Keyless Domain-Wide Delegation via the IAM Service Account Credentials API.
        #
        # Application Default Credentials on Cloud Run come from the metadata server
        # (google.auth.compute_engine.Credentials). Those credentials have no
        # `with_subject()`, so they cannot perform DWD impersonation on their own -
        # calling the Directory API with them fails with "404 Domain not found" because
        # the bare service account belongs to no Workspace domain.
        #
        # Instead, use the runtime service account to sign a JWT that asserts the
        # delegated-admin `subject`, then exchange it for an access token. Requirements:
        #   * iamcredentials.googleapis.com enabled on the project, and
        #   * the runtime service account holding roles/iam.serviceAccountTokenCreator
        #     on itself (so it can call signBlob).
        try:
            source_creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except Exception as e:
            logger.error("Could not obtain default credentials: %s", e)
            raise RuntimeError(
                "No valid Google credentials found. Set SERVICE_ACCOUNT_KEY_JSON / "
                "SERVICE_ACCOUNT_KEY_PATH, or run on GCP with a service account attached."
            ) from e

        if not subject:
            # No delegated admin configured; hand back the raw ADC credentials.
            return source_creds

        sa_email = settings.RUNTIME_SERVICE_ACCOUNT_EMAIL or getattr(
            source_creds, "service_account_email", None
        )
        if not sa_email or sa_email == "default":
            raise RuntimeError(
                "Domain-Wide Delegation requires a concrete runtime service account email. "
                "Set the RUNTIME_SERVICE_ACCOUNT_EMAIL environment variable to the Cloud Run "
                "service account (e.g. <sa-name>@<project-id>.iam.gserviceaccount.com)."
            )

        try:
            signer = iam.Signer(Request(), source_creds, sa_email)
            return service_account.Credentials(
                signer=signer,
                service_account_email=sa_email,
                token_uri=_OAUTH_TOKEN_URI,
                scopes=scopes,
                subject=subject,
            )
        except Exception as e:
            logger.error("Failed to build delegated credentials via IAM Credentials API: %s", e)
            raise RuntimeError(
                f"Could not impersonate '{subject}' via Domain-Wide Delegation. Ensure the IAM "
                "Service Account Credentials API (iamcredentials.googleapis.com) is enabled and "
                f"that the runtime service account '{sa_email}' has "
                "roles/iam.serviceAccountTokenCreator on itself."
            ) from e

    def get_directory_service(self, subject_email: Optional[str] = None):
        """Construct Google Workspace Admin SDK Directory API client."""
        creds = self.get_credentials(subject_email)
        return build("admin", "directory_v1", credentials=creds, cache_discovery=False)

    def get_gmail_service(self, subject_email: Optional[str] = None):
        """Construct a Gmail API client that sends mail as the impersonated user.

        Requires the gmail.send scope to be authorized for the service account in
        the Workspace Admin Console (Domain-Wide Delegation).
        """
        creds = self.get_credentials(subject_email, scopes=[GMAIL_SEND_SCOPE])
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def test_dwd_connection(self, subject_email: Optional[str] = None) -> Dict[str, Any]:
        """Perform a live connectivity and permission test against the Directory API.
        
        Verifies that:
        1. Service Account credentials can be parsed.
        2. DWD impersonation succeeds with the given admin email.
        3. Scopes are granted in Google Workspace Admin Console.
        """
        subject = subject_email or self.delegated_admin_email
        try:
            service = self.get_directory_service(subject)
            # Query groups with maxResults=1 to verify group.readonly scope & DWD
            result = service.groups().list(customer="my_customer", maxResults=1).execute()
            groups_found = len(result.get("groups", []))
            return {
                "success": True,
                "message": f"Successfully connected to Admin SDK Directory API. DWD authenticated as {subject}.",
                "subject": subject,
                "groups_sample_count": groups_found,
            }
        except HttpError as err:
            logger.error("HTTP error during DWD connection test: %s", err)
            status_code = err.resp.status
            reason = err.reason
            guidance = ""
            if status_code == 403 or "unauthorized_client" in str(err).lower():
                guidance = (
                    "Client not authorized. Ensure the Service Account Client ID is added to "
                    "Google Admin Console > Security > Access and data control > API controls > Domain-wide delegation "
                    "with scope 'https://www.googleapis.com/auth/admin.directory.group.readonly'."
                )
            elif status_code == 400:
                guidance = f"Bad request. Ensure '{subject}' is an active admin user in the domain."
            
            return {
                "success": False,
                "status_code": status_code,
                "message": f"API Error ({status_code}): {reason}",
                "guidance": guidance,
                "subject": subject,
            }
        except Exception as ex:
            logger.error("Unexpected error testing DWD connection: %s", ex)
            return {
                "success": False,
                "message": f"Connection failed: {str(ex)}",
                "subject": subject,
                "guidance": "Check that the service account key or environment variables are set correctly."
            }

    def list_domain_groups(self, subject_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve all Google Groups in the Workspace domain."""
        service = self.get_directory_service(subject_email)
        groups = []
        page_token = None

        while True:
            response = service.groups().list(
                customer="my_customer",
                maxResults=200,
                pageToken=page_token
            ).execute()
            
            for item in response.get("groups", []):
                groups.append({
                    "id": item.get("id"),
                    "email": item.get("email"),
                    "name": item.get("name"),
                    "description": item.get("description", ""),
                    "directMembersCount": item.get("directMembersCount", 0)
                })
                
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # Sort alphabetically by name
        groups.sort(key=lambda g: g.get("name", "").lower())
        return groups

    def list_direct_group_members(self, group_key: str, subject_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """List direct members of a Google Group (no recursive traversal)."""
        service = self.get_directory_service(subject_email)
        members = []
        page_token = None

        while True:
            try:
                response = service.members().list(
                    groupKey=group_key,
                    maxResults=200,
                    pageToken=page_token
                ).execute()
            except HttpError as e:
                logger.error("Failed to list members for group %s: %s", group_key, e)
                raise

            for m in response.get("members", []):
                members.append({
                    "id": m.get("id"),
                    "email": m.get("email"),
                    "type": m.get("type"),    # "USER", "GROUP", "CUSTOMER"
                    "role": m.get("role"),    # "MEMBER", "MANAGER", "OWNER"
                    "status": m.get("status") # "ACTIVE", "SUSPENDED"
                })

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return members
