import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from google.cloud import firestore
from app.config import settings

logger = logging.getLogger("gemini_provisioner.firestore")

_db_client: Optional[firestore.Client] = None


def get_firestore_client() -> firestore.Client:
    """Singleton helper to obtain Firestore client."""
    global _db_client
    if _db_client is None:
        try:
            _db_client = firestore.Client(
                project=settings.GCP_PROJECT_ID,
                database=settings.FIRESTORE_DATABASE
            )
            logger.info("Initialized Firestore client for project %s", settings.GCP_PROJECT_ID)
        except Exception as e:
            logger.error("Failed to initialize Firestore client: %s", e)
            raise
    return _db_client


def get_config() -> Dict[str, Any]:
    """Retrieve application configuration from Firestore, returning defaults if not yet created."""
    default_config: Dict[str, Any] = {
        "monitored_groups": [],
        # Selected Gemini Enterprise license subscription (Discovery Engine
        # license config resource name) + a human label for display.
        "license_config": settings.LICENSE_CONFIG or "",
        "license_label": "",
        "delegated_admin_email": settings.DELEGATED_ADMIN_EMAIL,
        "cron_expression": "0 2 * * *",
        # Run notifications
        "notification_emails": [],          # list of recipient addresses
        "notify_on": "failures",            # "failures" (only FAILED/PARTIAL_SUCCESS) or "all"
        "public_base_url": settings.PUBLIC_BASE_URL or "",
        "last_updated": None,
    }
    
    try:
        db = get_firestore_client()
        doc_ref = db.collection(settings.CONFIG_COLLECTION).document(settings.CONFIG_DOC_ID)
        snapshot = doc_ref.get()
        if snapshot.exists:
            data = snapshot.to_dict() or {}
            # Merge with defaults to ensure all keys exist
            return {**default_config, **data}
        else:
            logger.info("No config document found in Firestore. Creating default...")
            doc_ref.set(default_config)
            return default_config
    except Exception as e:
        logger.warning("Error fetching config from Firestore (%s). Using fallback defaults.", e)
        return default_config


def update_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update settings in Firestore and return full current configuration."""
    db = get_firestore_client()
    doc_ref = db.collection(settings.CONFIG_COLLECTION).document(settings.CONFIG_DOC_ID)
    
    current = get_config()
    current.update(updates)
    current["last_updated"] = datetime.now(timezone.utc).isoformat()
    
    doc_ref.set(current)
    logger.info("Successfully updated Firestore config: %s", list(updates.keys()))
    return current


def record_sync_history(run_record: Dict[str, Any]) -> str:
    """Record a sync run record in Firestore sync_history collection."""
    db = get_firestore_client()
    col_ref = db.collection(settings.HISTORY_COLLECTION)
    
    if "created_at" not in run_record:
        run_record["created_at"] = datetime.now(timezone.utc).isoformat()
        
    doc_ref = col_ref.document()
    doc_ref.set(run_record)
    logger.info("Saved sync history document ID %s (status: %s)", doc_ref.id, run_record.get("status"))
    return doc_ref.id


def get_sync_history(limit: int = 25) -> List[Dict[str, Any]]:
    """Retrieve recent sync execution history sorted by start time descending."""
    try:
        db = get_firestore_client()
        col_ref = db.collection(settings.HISTORY_COLLECTION)
        query = col_ref.order_by("started_at", direction=firestore.Query.DESCENDING).limit(limit)
        docs = query.stream()
        
        history = []
        for d in docs:
            record = d.to_dict()
            record["id"] = d.id
            history.append(record)
        return history
    except Exception as e:
        logger.error("Error retrieving sync history from Firestore: %s", e)
        return []
