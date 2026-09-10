import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Set

from app.config import settings
from app.firestore_db import get_config, record_sync_history
from app.workspace_client import WorkspaceClient
from app.gemini_licensing import GeminiLicenseClient, is_valid_config_name, location_from_config_name
from app.notifications import send_sync_notification

logger = logging.getLogger("gemini_provisioner.sync")


def _notify(config: Dict[str, Any], run_record: Dict[str, Any]) -> None:
    """Best-effort run notification; never lets a mail error escape."""
    try:
        result = send_sync_notification(config, run_record)
        run_record["notification"] = result
    except Exception as e:  # pragma: no cover - defensive
        logger.error("Notification dispatch failed: %s", e)
        run_record["notification"] = {"sent": False, "reason": str(e)}


def _finalize(config: Dict[str, Any], run_record: Dict[str, Any]) -> Dict[str, Any]:
    try:
        run_record["doc_id"] = record_sync_history(run_record)
    except Exception as e:
        logger.error("Could not write sync record to Firestore: %s", e)
    _notify(config, run_record)
    return run_record


def run_license_sync(triggered_by: str = "scheduled") -> Dict[str, Any]:
    """Core synchronization engine.

    1. Read monitored groups and the selected Gemini Enterprise license config.
    2. Query direct group members (flat, no nesting); flag nested groups.
    3. Deduplicate user emails.
    4. Skip users who already hold a license from that config; assign the rest via
       the Discovery Engine batchUpdateUserLicenses API (additive only).
    5. Persist audit stats to Cloud Logging + Firestore, and notify.
    """
    start_time = datetime.now(timezone.utc)
    start_perf = time.perf_counter()
    logger.info("Starting Gemini Enterprise license sync (triggered by: %s)", triggered_by)

    config = get_config()
    monitored_groups: List[str] = config.get("monitored_groups", [])
    delegated_email: str = config.get("delegated_admin_email", settings.DELEGATED_ADMIN_EMAIL)
    license_config: str = (config.get("license_config") or "").strip()
    license_label: str = config.get("license_label") or license_config

    def base_record(**extra: Any) -> Dict[str, Any]:
        rec = {
            "started_at": start_time.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.perf_counter() - start_perf, 2),
            "triggered_by": triggered_by,
            "license_config": license_config,
            "license_label": license_label,
            "monitored_groups_count": len(monitored_groups),
            "monitored_groups": monitored_groups,
            "evaluated_users_count": 0,
            "licenses_assigned_count": 0,
            "licenses_already_held_count": 0,
            "nested_groups_count": 0,
            "errors_count": 0,
            "errors": [],
        }
        rec.update(extra)
        return rec

    # --- guards ---------------------------------------------------------------
    if not license_config:
        logger.error("No Gemini Enterprise license subscription selected in configuration.")
        return _finalize(config, base_record(
            status="FAILED",
            errors_count=1,
            errors=[{"item": "config", "type": "NO_LICENSE_CONFIG",
                     "error": "No Gemini Enterprise license subscription is selected on the Settings page."}],
            message="No license subscription configured.",
        ))

    if not is_valid_config_name(license_config):
        logger.error("Configured license_config is not a Gemini Enterprise license resource: %s", license_config)
        return _finalize(config, base_record(
            status="FAILED",
            errors_count=1,
            errors=[{"item": license_config, "type": "INVALID_LICENSE_CONFIG",
                     "error": ("Selected value is not a Gemini Enterprise license config "
                               "(projects/*/locations/*/licenseConfigs/*). Workspace product/SKU "
                               "IDs are not supported.")}],
            message="Invalid license subscription.",
        ))

    if not monitored_groups:
        logger.warning("No Google Groups configured for monitoring in Firestore.")
        return _finalize(config, base_record(status="SUCCESS", message="No groups configured for monitoring."))

    location = location_from_config_name(license_config)
    ws = WorkspaceClient(delegated_admin_email=delegated_email)
    gem = GeminiLicenseClient()

    errors: List[Dict[str, Any]] = []
    nested_groups_skipped: List[Dict[str, str]] = []
    unique_user_emails: Set[str] = set()

    # --- Step 1: gather direct members ------------------------------------
    for group_email in monitored_groups:
        logger.info("Inspecting members for group: %s", group_email)
        try:
            members = ws.list_direct_group_members(group_email)
        except Exception as e:
            err_msg = f"Failed to retrieve members for group '{group_email}': {e}"
            logger.error(err_msg)
            errors.append({"item": group_email, "type": "GROUP_QUERY_ERROR", "error": err_msg})
            continue

        for m in members:
            m_type = m.get("type", "").upper()
            m_email = (m.get("email") or "").strip().lower()
            if not m_email:
                continue
            if m_type == "GROUP":
                err_msg = (
                    f"Group '{group_email}' contains nested group '{m_email}'. "
                    f"Nested groups are not supported for licensing. Only direct user accounts can be provisioned."
                )
                logger.warning(err_msg)
                nested_groups_skipped.append(
                    {"parent_group": group_email, "nested_group_email": m_email, "error": err_msg}
                )
                errors.append({"item": m_email, "type": "NESTED_GROUP_UNSUPPORTED", "error": err_msg})
            elif m_type == "USER":
                unique_user_emails.add(m_email)
            else:
                logger.info("Ignoring non-user member type '%s': %s", m_type, m_email)

    # --- Step 2: skip already-licensed, assign the rest -----------------
    assigned_count = 0
    already_held_count = 0

    logger.info("Evaluating %d unique user(s) against license config %s (%s)",
                len(unique_user_emails), license_config, location)

    try:
        already_licensed = gem.assigned_user_emails(location)
    except Exception as e:
        err_msg = f"Could not list existing Gemini Enterprise licenses ({location}): {e}"
        logger.error(err_msg)
        errors.append({"item": license_config, "type": "LICENSE_LIST_ERROR", "error": err_msg})
        already_licensed = set()

    to_assign = sorted(e for e in unique_user_emails if e not in already_licensed)
    already_held_count = len(unique_user_emails) - len(to_assign)
    if already_held_count:
        logger.info("%d user(s) already hold a license from this subscription — skipping.", already_held_count)

    if to_assign:
        logger.info("Assigning license to %d user(s): %s", len(to_assign),
                    ", ".join(to_assign[:10]) + (" …" if len(to_assign) > 10 else ""))
        try:
            result = gem.batch_assign(license_config, to_assign)
            assigned_count = result.get("assigned", 0)
            for msg in result.get("errors", []):
                errors.append({"item": license_config, "type": "LICENSE_ASSIGN_FAILED", "error": msg})
            if result.get("failed") and not result.get("errors"):
                errors.append({
                    "item": license_config, "type": "LICENSE_ASSIGN_FAILED",
                    "error": f"{result['failed']} assignment(s) failed (operation {result.get('operation')}).",
                })
        except Exception as e:
            err_msg = f"batchUpdateUserLicenses failed: {e}"
            logger.error(err_msg)
            errors.append({"item": license_config, "type": "LICENSE_ASSIGN_FAILED", "error": err_msg})

    # --- finalize ------------------------------------------------------------
    if not errors:
        status = "SUCCESS"
    elif assigned_count > 0 or already_held_count > 0:
        status = "PARTIAL_SUCCESS"
    else:
        status = "FAILED"

    run_record = base_record(
        status=status,
        evaluated_users_count=len(unique_user_emails),
        licenses_assigned_count=assigned_count,
        licenses_already_held_count=already_held_count,
        nested_groups_count=len(nested_groups_skipped),
        errors_count=len(errors),
        errors=errors[:50],
    )

    _finalize(config, run_record)

    logger.info(
        "Sync completed in %.2fs. Evaluated: %d, Assigned: %d, Already Held: %d, Nested Groups Skipped: %d, Errors: %d",
        run_record["duration_seconds"], len(unique_user_emails), assigned_count,
        already_held_count, len(nested_groups_skipped), len(errors),
    )
    return run_record
