import logging
import os
import re
from typing import Dict, Any, List, Optional, Union
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings
from app.firestore_db import get_config, update_config, get_sync_history
from app.workspace_client import WorkspaceClient
from app.sync_worker import run_license_sync
from app.scheduler_service import SchedulerService
from app import auth
from app.auth import require_super_admin, require_sync_caller

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("gemini_provisioner.main")

# When access control is enforced, hide the interactive API docs / schema behind
# IAP is still not enough - drop them entirely so only real endpoints are exposed.
_docs_kwargs = {} if not settings.AUTH_ENABLED else {
    "docs_url": None, "redoc_url": None, "openapi_url": None,
}

app = FastAPI(
    title="Gemini Enterprise License Provisioner",
    description="Automate Gemini Enterprise license assignment based on Google Groups membership.",
    version="1.0.0",
    **_docs_kwargs,
)

# Paths for static and templates
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Expose deployment identifiers to every template (shown in the header/footer).
templates.env.globals["gcp_project_id"] = settings.GCP_PROJECT_ID or "unset"
templates.env.globals["gcp_region"] = settings.GCP_REGION

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Last public base URL persisted to Firestore config (per process cache, so we
# only write when it actually changes).
_seen_base_url: Optional[str] = None


@app.middleware("http")
async def capture_base_url(request: Request, call_next):
    """Learn this service's public base URL from real traffic so scheduler-triggered
    runs (which have no request) can build absolute links in notification emails.

    Only ``*.run.app`` hosts are auto-trusted (the client-controlled Host header
    could otherwise poison the link). For a custom domain, set ``PUBLIC_BASE_URL``.
    """
    global _seen_base_url
    # Log the IAP JWT audience even before enforcement is turned on, so the exact
    # value for IAP_AUDIENCE is discoverable from the logs. No-op without the header.
    _assertion = request.headers.get(settings.IAP_JWT_HEADER)
    if _assertion:
        auth._log_observed_audience(_assertion)
    try:
        if not settings.PUBLIC_BASE_URL and request.method == "GET" and \
                not request.url.path.startswith(("/static", "/healthz", "/api")):
            base = str(request.base_url).rstrip("/")
            host = request.url.hostname or ""
            trusted = host.endswith(".run.app") or host in ("localhost", "127.0.0.1")
            if base and trusted and base != _seen_base_url:
                _seen_base_url = base
                if get_config().get("public_base_url") != base:
                    update_config({"public_base_url": base})
    except Exception as e:  # never break a request over this
        logger.debug("base URL capture skipped: %s", e)
    return await call_next(request)


# -------------------------------------------------------------------------
# Request Schemas
# -------------------------------------------------------------------------
class GroupsPayload(BaseModel):
    groups: List[str]


class SchedulePayload(BaseModel):
    cron_expression: str


class NotificationsPayload(BaseModel):
    # Accept a list or a raw comma/newline separated string from the form.
    notification_emails: Union[List[str], str] = []
    notify_on: str = "failures"  # "failures" or "all"


class SettingsPayload(BaseModel):
    delegated_admin_email: str
    product_id: str
    sku_id: str


class SyncTriggerPayload(BaseModel):
    triggered_by: Optional[str] = "admin_ui"


class DwdTestPayload(BaseModel):
    delegated_admin_email: Optional[str] = None


# -------------------------------------------------------------------------
# HTML Views
# -------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard_view(request: Request, principal: Optional[str] = Depends(require_super_admin)):
    """Admin Dashboard Homepage."""
    config = get_config()
    history = get_sync_history(limit=1)
    last_run = history[0] if history else None

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "config": config,
            "last_run": last_run,
            "principal": principal,
        }
    )


@app.get("/groups", response_class=HTMLResponse)
async def groups_view(request: Request, principal: Optional[str] = Depends(require_super_admin)):
    """Google Groups selection view."""
    config = get_config()
    monitored = config.get("monitored_groups", [])
    delegated_email = config.get("delegated_admin_email", settings.DELEGATED_ADMIN_EMAIL)
    
    domain_groups = []
    error_msg = None
    try:
        client = WorkspaceClient(delegated_admin_email=delegated_email)
        domain_groups = client.list_domain_groups()
    except Exception as e:
        logger.error("Failed to query domain groups: %s", e)
        error_msg = str(e)

    return templates.TemplateResponse(
        request=request,
        name="groups.html",
        context={
            "active_page": "groups",
            "monitored_groups": monitored,
            "domain_groups": domain_groups,
            "error": error_msg,
            "principal": principal,
        }
    )


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_view(request: Request, principal: Optional[str] = Depends(require_super_admin)):
    """Sync schedule configuration view."""
    config = get_config()
    scheduler_service = SchedulerService()
    scheduler_status = scheduler_service.get_schedule()

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "active_page": "schedule",
            "config": config,
            "scheduler_status": scheduler_status,
            "principal": principal,
        }
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request, principal: Optional[str] = Depends(require_super_admin)):
    """System settings and DWD connectivity test view."""
    config = get_config()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "config": config,
            "principal": principal,
        }
    )


@app.get("/history", response_class=HTMLResponse)
async def history_view(request: Request, principal: Optional[str] = Depends(require_super_admin)):
    """Execution audit history view."""
    history = get_sync_history(limit=50)
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "active_page": "history",
            "history": history,
            "principal": principal,
        }
    )


# -------------------------------------------------------------------------
# API Endpoints
# -------------------------------------------------------------------------
@app.post("/api/groups")
async def save_monitored_groups(payload: GroupsPayload, _: Optional[str] = Depends(require_super_admin)):
    """Save selected Google Groups to monitor in Firestore."""
    try:
        updated = update_config({"monitored_groups": payload.groups})
        return {
            "success": True,
            "message": f"Successfully updated monitored groups ({len(payload.groups)} selected).",
            "monitored_groups": updated.get("monitored_groups", [])
        }
    except Exception as e:
        logger.error("Error saving groups to Firestore: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/schedule")
async def update_sync_schedule(payload: SchedulePayload, _: Optional[str] = Depends(require_super_admin)):
    """Update cron schedule in Firestore and programmatically in Cloud Scheduler."""
    cron = payload.cron_expression.strip()
    if not cron:
        raise HTTPException(status_code=400, detail="Cron expression cannot be empty.")

    # 1. Update Firestore config
    try:
        update_config({"cron_expression": cron})
    except Exception as e:
        logger.error("Failed to save schedule to Firestore: %s", e)

    # 2. Update Cloud Scheduler job
    scheduler_svc = SchedulerService()
    sched_result = scheduler_svc.update_schedule(cron)

    return {
        "success": True,
        "cron_expression": cron,
        "cloud_scheduler": sched_result,
        "message": f"Schedule set to '{cron}' in Firestore. Cloud Scheduler: {sched_result.get('message')}"
    }


@app.post("/api/notifications")
async def update_notifications(payload: NotificationsPayload, _: Optional[str] = Depends(require_super_admin)):
    """Save sync-run email notification settings to Firestore."""
    raw = payload.notification_emails
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    emails = [e.strip() for e in raw if e and e.strip()]

    invalid = [e for e in emails if not _EMAIL_RE.match(e)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid email address(es): {', '.join(invalid)}")

    notify_on = payload.notify_on.strip().lower()
    if notify_on not in ("failures", "all"):
        raise HTTPException(status_code=400, detail="notify_on must be 'failures' or 'all'.")

    try:
        updated = update_config({
            "notification_emails": emails,
            "notify_on": notify_on,
        })
    except Exception as e:
        logger.error("Failed to save notification settings: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    scope = "all runs" if notify_on == "all" else "failed & partial runs only"
    msg = (
        f"Notifications saved: {len(emails)} recipient(s), alerting on {scope}."
        if emails else "Notifications disabled (no recipients)."
    )
    return {
        "success": True,
        "message": msg,
        "notification_emails": updated.get("notification_emails", []),
        "notify_on": updated.get("notify_on", "failures"),
    }


@app.post("/api/settings")
async def save_settings(payload: SettingsPayload, _: Optional[str] = Depends(require_super_admin)):
    """Update Delegated Admin Email, Product ID, and SKU ID in Firestore."""
    try:
        updated = update_config({
            "delegated_admin_email": payload.delegated_admin_email.strip(),
            "product_id": payload.product_id.strip(),
            "sku_id": payload.sku_id.strip(),
        })
        return {
            "success": True,
            "message": "Settings saved successfully.",
            "config": updated
        }
    except Exception as e:
        logger.error("Failed to save settings: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test-connection")
async def test_dwd_connection(payload: DwdTestPayload, _: Optional[str] = Depends(require_super_admin)):
    """Perform live connectivity check against Admin SDK Directory API using DWD."""
    client = WorkspaceClient(delegated_admin_email=payload.delegated_admin_email)
    result = client.test_dwd_connection(payload.delegated_admin_email)
    return result


@app.post("/api/sync/run")
async def trigger_sync(
    request: Request,
    payload: Optional[SyncTriggerPayload] = None,
    caller: Optional[str] = Depends(require_sync_caller),
):
    """Scheduled & manual sync worker trigger endpoint.

    Invoked by:
    - Cloud Scheduler via HTTP POST with OIDC authentication
    - Admin UI manually via JSON POST

    When IAP is enforced, the caller must be a Workspace super admin or the
    configured scheduler service account (SYNC_INVOKER_SA_EMAIL).
    """
    # Identify trigger source
    auth_header = request.headers.get("Authorization", "")
    user_agent = request.headers.get("User-Agent", "")
    invoker_sa = (settings.SYNC_INVOKER_SA_EMAIL or "").strip().lower()

    triggered_by = "admin_ui"
    if caller and invoker_sa and caller == invoker_sa:
        triggered_by = "scheduled"
    elif caller:
        triggered_by = "admin_ui"
    elif "Google-Cloud-Scheduler" in user_agent or "Bearer" in auth_header:
        triggered_by = "scheduled"
    elif payload and payload.triggered_by:
        triggered_by = payload.triggered_by

    logger.info("Executing license sync endpoint (trigger source: %s)", triggered_by)

    try:
        result = run_license_sync(triggered_by=triggered_by)
        return JSONResponse(content=result, status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.critical("Sync engine crashed: %s", e, exc_info=True)
        return JSONResponse(
            content={
                "status": "FAILED",
                "triggered_by": triggered_by,
                "error": str(e),
                "message": "Sync engine encountered an unhandled exception."
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@app.get("/healthz")
async def healthz():
    """Liveness/readiness probe for Cloud Run."""
    return {"status": "healthy", "service": "gemini-license-provisioner"}
