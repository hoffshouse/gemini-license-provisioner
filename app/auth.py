"""Access control: Identity-Aware Proxy (IAP) authentication + a Google Workspace
*super administrator* authorization check.

Enforcement is on whenever ``settings.IAP_AUDIENCE`` is set. In that mode every
page and every mutating API requires:

  1. a valid IAP JWT assertion (header ``x-goog-iap-jwt-assertion``), verified
     against Google's public keys with the configured audience, and
  2. the asserted email must be an ``isAdmin`` (super admin), non-suspended user
     in the Workspace directory - looked up via the same DWD client used for
     provisioning, and cached briefly.

The scheduler's service account is allowed through ``POST /api/sync/run`` only
(``SYNC_INVOKER_SA_EMAIL``). A small break-glass allow-list
(``AUTH_BOOTSTRAP_ADMINS``) is honoured for all routes.

When ``IAP_AUDIENCE`` is unset the dependencies are no-ops, preserving the
pre-IAP (open) behaviour for local dev.
"""
import logging
import time
from typing import Dict, Optional

from fastapi import HTTPException, Request, status
from google.auth import jwt as ga_jwt
from google.auth.transport import requests as ga_requests
from google.oauth2 import id_token

from app.config import settings
from app.workspace_client import WorkspaceClient

logger = logging.getLogger("gemini_provisioner.auth")

_IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_IAP_ISSUER = "https://cloud.google.com/iap"

_ga_request = ga_requests.Request()
# email -> (is_admin, expires_at_epoch)
_admin_cache: Dict[str, tuple] = {}

_warned_open = False
_seen_auds: set = set()


def _warn_open_once() -> None:
    global _warned_open
    if not _warned_open:
        logger.warning(
            "IAP_AUDIENCE is not set - the admin UI and APIs are UNAUTHENTICATED. "
            "See setup_instructions.md -> Security Model."
        )
        _warned_open = True


def _log_observed_audience(assertion: str) -> None:
    """Log the aud/email of an incoming IAP assertion (unverified) once per distinct
    aud, so the exact value to put in IAP_AUDIENCE is discoverable from the logs."""
    try:
        claims = ga_jwt.decode(assertion, verify=False)
        aud = claims.get("aud")
        if aud and aud not in _seen_auds:
            _seen_auds.add(aud)
            logger.warning(
                "IAP assertion received: aud=%r email=%r. Set IAP_AUDIENCE to this aud "
                "value if it differs from the configured one.", aud, claims.get("email"),
            )
    except Exception:  # never let diagnostics break the request
        pass


def _verify_iap_assertion(assertion: str) -> Dict:
    try:
        payload = id_token.verify_token(
            assertion,
            _ga_request,
            audience=settings.IAP_AUDIENCE,
            certs_url=_IAP_CERTS_URL,
        )
    except Exception as e:  # signature / audience / expiry
        logger.warning("IAP assertion verification failed: %s", e)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid IAP assertion.")
    if payload.get("iss") != _IAP_ISSUER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unexpected IAP issuer.")
    return payload


def is_super_admin(email: str) -> bool:
    """True if `email` is a non-suspended Workspace super administrator.

    Result is cached for SUPER_ADMIN_CACHE_TTL seconds; lookup failures fail
    closed and are cached only briefly so a transient Directory outage recovers.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    if email in settings.bootstrap_admin_emails:
        return True

    now = time.time()
    cached = _admin_cache.get(email)
    if cached and cached[1] > now:
        return cached[0]

    try:
        service = WorkspaceClient().get_directory_service()
        user = service.users().get(userKey=email, fields="isAdmin,suspended").execute()
        result = bool(user.get("isAdmin")) and not user.get("suspended", False)
        _admin_cache[email] = (result, now + settings.SUPER_ADMIN_CACHE_TTL)
        return result
    except Exception as e:
        logger.error("Super-admin lookup failed for %s: %s", email, e)
        _admin_cache[email] = (False, now + 15)
        return False


def current_principal(request: Request) -> Optional[str]:
    """Verified caller email, or None when access control is disabled."""
    if not settings.AUTH_ENABLED:
        _warn_open_once()
        return None
    assertion = request.headers.get(settings.IAP_JWT_HEADER)
    if not assertion:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing IAP assertion header. Requests must arrive through Identity-Aware Proxy.",
        )
    email = (_verify_iap_assertion(assertion).get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="IAP assertion carries no email.")
    request.state.principal = email
    return email


def require_super_admin(request: Request) -> Optional[str]:
    """FastAPI dependency: allow only Workspace super admins. No-op if auth disabled."""
    email = current_principal(request)
    if email is None:
        return None
    if not is_super_admin(email):
        logger.warning("Access denied for %s: not a Workspace super administrator", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires Google Workspace super administrator access.",
        )
    return email


def require_sync_caller(request: Request) -> Optional[str]:
    """Dependency for POST /api/sync/run: a super admin, or the scheduler SA."""
    email = current_principal(request)
    if email is None:
        return None
    invoker = (settings.SYNC_INVOKER_SA_EMAIL or "").strip().lower()
    if invoker and email == invoker:
        return email
    if is_super_admin(email):
        return email
    logger.warning("Sync trigger denied for %s", email)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to trigger a sync run.",
    )
