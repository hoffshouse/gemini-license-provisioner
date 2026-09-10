"""Gemini Enterprise license management via the Discovery Engine API.

This is a Google **Cloud** API (`discoveryengine.googleapis.com`), not a Workspace
API — it is called with the runtime service account's own credentials (no
Domain-Wide Delegation). The service account needs `roles/discoveryengine.admin`
(or an equivalent custom role) on the project.

Concepts:
  * A **license config** is a subscription/pool, e.g.
    `projects/<PROJECT_NUMBER>/locations/<LOCATION>/licenseConfigs/<ID>` — this is
    what an admin picks in the console. `LOCATION` is one of `global`, `us`, `eu`
    and selects the regional endpoint (`<location>-discoveryengine.googleapis.com`).
  * A **user license** ties a `userPrincipal` (email) to a license config with an
    assignment state (`ASSIGNED`, `NO_LICENSE`, `BLOCKED`).
"""
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set

import google.auth
from google.auth.transport.requests import AuthorizedSession

from app.config import settings

logger = logging.getLogger("gemini_provisioner.gemini_licensing")

# Regional endpoints Discovery Engine license configs can live in.
LICENSE_LOCATIONS = ("global", "us", "eu")
_USER_STORE = "default_user_store"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_CONFIG_NAME_RE = re.compile(
    r"^projects/[^/]+/locations/(?P<location>global|us|eu)/licenseConfigs/[^/]+$"
)

_TIER_LABELS = {
    "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT": "Gemini Enterprise (Search + Assistant)",
    "SUBSCRIPTION_TIER_SEARCH": "Gemini Enterprise (Search)",
    "SUBSCRIPTION_TIER_NOTEBOOK_LM": "NotebookLM Enterprise",
    "SUBSCRIPTION_TIER_FRONTLINE_WORKER": "Gemini Enterprise (Frontline)",
    "SUBSCRIPTION_TIER_STANDARD": "Gemini Enterprise Standard",
    "SUBSCRIPTION_TIER_PLUS": "Gemini Enterprise Plus",
}


def location_from_config_name(name: str) -> Optional[str]:
    m = _CONFIG_NAME_RE.match(name or "")
    return m.group("location") if m else None


def is_valid_config_name(name: str) -> bool:
    return bool(_CONFIG_NAME_RE.match(name or ""))


def _label(cfg: Dict[str, Any]) -> str:
    tier = _TIER_LABELS.get(cfg.get("subscriptionTier", ""), cfg.get("subscriptionTier", "Gemini Enterprise"))
    seats = cfg.get("licenseCount")
    bits = [tier]
    if seats:
        bits.append(f"{seats} seats")
    bits.append(cfg["_location"])
    if cfg.get("freeTrial"):
        bits.append("Free trial")
    state = cfg.get("state")
    label = " — ".join(bits)
    return f"{label} [{state}]" if state else label


class GeminiLicenseClient:
    """Thin REST client for Discovery Engine license configs and user licenses."""

    def __init__(self, project_id: Optional[str] = None):
        self.project_id = project_id or settings.GCP_PROJECT_ID
        self._session: Optional[AuthorizedSession] = None

    # -- infra ---------------------------------------------------------------
    def _sess(self) -> AuthorizedSession:
        if self._session is None:
            creds, _ = google.auth.default(scopes=_SCOPES)
            self._session = AuthorizedSession(creds)
        return self._session

    def _base(self, location: str) -> str:
        return f"https://{location}-discoveryengine.googleapis.com/v1"

    def _headers(self) -> Dict[str, str]:
        return {"X-Goog-User-Project": self.project_id, "Content-Type": "application/json"}

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self._sess().get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _post(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._sess().post(url, headers=self._headers(), json=body, timeout=60)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # -- license configs (the "subscriptions" a user picks) ----------------
    def list_license_configs(self) -> List[Dict[str, Any]]:
        """Every ACTIVE-or-otherwise license config across all regional endpoints,
        each with a ready-to-display ``label`` and parsed ``location``."""
        out: List[Dict[str, Any]] = []
        for loc in LICENSE_LOCATIONS:
            url = f"{self._base(loc)}/projects/{self.project_id}/locations/{loc}/licenseConfigs"
            try:
                data = self._get(url)
            except Exception as e:  # a location with nothing configured 404s / 403s
                logger.debug("licenseConfigs list failed for location %s: %s", loc, e)
                continue
            for cfg in data.get("licenseConfigs", []):
                cfg["_location"] = loc
                cfg["label"] = _label(cfg)
                out.append(cfg)
        out.sort(key=lambda c: (not c.get("freeTrial", False), c["label"]))
        return out

    # -- user licenses -----------------------------------------------------
    def assigned_user_emails(self, location: str) -> Set[str]:
        """Lower-cased emails currently in state ASSIGNED for the given location."""
        emails: Set[str] = set()
        url = (f"{self._base(location)}/projects/{self.project_id}/locations/{location}"
               f"/userStores/{_USER_STORE}/userLicenses")
        page_token = None
        while True:
            params = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            data = self._get(url, params=params)
            for ul in data.get("userLicenses", []):
                if ul.get("licenseAssignmentState") == "ASSIGNED" and ul.get("userPrincipal"):
                    emails.add(ul["userPrincipal"].strip().lower())
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return emails

    def batch_assign(self, license_config_name: str, emails: List[str],
                     poll_timeout: int = 180) -> Dict[str, Any]:
        """Assign `license_config_name` to `emails` via batchUpdateUserLicenses.

        Additive: `deleteUnassignedUserLicenses=false`, so users not listed are
        untouched. Returns {assigned, failed, errors, operation}.
        """
        location = location_from_config_name(license_config_name)
        if not location:
            raise ValueError(f"Not a license config resource name: {license_config_name!r}")
        if not emails:
            return {"assigned": 0, "failed": 0, "errors": [], "operation": None}

        url = (f"{self._base(location)}/projects/{self.project_id}/locations/{location}"
               f"/userStores/{_USER_STORE}:batchUpdateUserLicenses")
        body = {
            "inlineSource": {
                "userLicenses": [
                    {"userPrincipal": e, "licenseConfig": license_config_name} for e in emails
                ],
                "updateMask": "userPrincipal,licenseConfig",
            },
            "deleteUnassignedUserLicenses": False,
        }
        op = self._post(url, body)
        op = self._await_operation(location, op, poll_timeout)

        if op.get("error"):
            return {"assigned": 0, "failed": len(emails),
                    "errors": [op["error"].get("message", str(op["error"]))],
                    "operation": op.get("name")}

        meta = op.get("metadata", {}) or {}
        resp = op.get("response", {}) or {}
        success = int(meta.get("successCount", resp.get("successCount", 0)) or 0)
        failure = int(meta.get("failureCount", resp.get("failureCount", 0)) or 0)
        if not success and not failure:  # older shape: count what we asked for
            success = len(emails)
        errors = [s.get("message", str(s)) for s in resp.get("errorSamples", [])][:20]
        return {"assigned": success, "failed": failure, "errors": errors,
                "operation": op.get("name")}

    def _await_operation(self, location: str, op: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        name = op.get("name")
        if not name or op.get("done"):
            return op
        url = f"{self._base(location)}/{name}"
        deadline = time.time() + timeout
        delay = 2
        while time.time() < deadline:
            time.sleep(delay)
            op = self._get(url)
            if op.get("done"):
                return op
            delay = min(delay * 1.5, 15)
        logger.warning("batchUpdateUserLicenses operation %s did not finish in %ss", name, timeout)
        return op
