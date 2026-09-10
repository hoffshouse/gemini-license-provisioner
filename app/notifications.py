"""Email notifications for sync runs, sent via the Gmail API using Domain-Wide
Delegation (keyless, reusing the same credential path as the Directory calls).

Requires the ``https://www.googleapis.com/auth/gmail.send`` scope to be added to
the service account's Domain-Wide Delegation entry in the Workspace Admin Console.
Sending failures are logged and returned, never raised - a broken mailbox must not
fail a sync.
"""
import base64
import html
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List

from app.config import settings
from app.workspace_client import WorkspaceClient

logger = logging.getLogger("gemini_provisioner.notifications")

_FAILURE_STATUSES = {"FAILED", "PARTIAL_SUCCESS"}


def _recipients(config: Dict[str, Any]) -> List[str]:
    raw = config.get("notification_emails") or []
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    return [e.strip() for e in raw if e and e.strip()]


def should_notify(config: Dict[str, Any], run_record: Dict[str, Any]) -> bool:
    """True when this run should trigger an email given the saved preference."""
    if not _recipients(config):
        return False
    mode = (config.get("notify_on") or "failures").lower()
    if mode == "all":
        return True
    return str(run_record.get("status", "")).upper() in _FAILURE_STATUSES


def _history_url(config: Dict[str, Any]) -> str:
    base = (settings.PUBLIC_BASE_URL or config.get("public_base_url") or "").rstrip("/")
    return f"{base}/history" if base else "/history"


def _explain_why(run_record: Dict[str, Any]) -> str:
    trig = str(run_record.get("triggered_by", "unknown"))
    return {
        "scheduled": "Cloud Scheduler cron trigger",
        "admin_ui": "Manual run from the admin dashboard",
        "local_test": "Local test harness",
    }.get(trig, trig)


def build_message(config: Dict[str, Any], run_record: Dict[str, Any]) -> Dict[str, str]:
    """Return {subject, text, html} describing the run in full."""
    status = str(run_record.get("status", "UNKNOWN")).upper()
    assigned = run_record.get("licenses_assigned_count", 0)
    held = run_record.get("licenses_already_held_count", 0)
    evaluated = run_record.get("evaluated_users_count", 0)
    nested = run_record.get("nested_groups_count", 0)
    errors = run_record.get("errors", []) or []
    err_count = run_record.get("errors_count", len(errors))
    groups = run_record.get("monitored_groups", []) or []
    started = run_record.get("started_at", "n/a")
    completed = run_record.get("completed_at", "n/a")
    duration = run_record.get("duration_seconds", "n/a")
    why = _explain_why(run_record)
    doc_id = run_record.get("doc_id", "")
    history_url = _history_url(config)

    subject = (
        f"[Gemini License Sync] {status} - "
        f"{assigned} assigned, {err_count} error(s)"
    )

    # ---- plain text ----
    subscription = run_record.get("license_label") or run_record.get("license_config") or "n/a"
    lines = [
        f"Gemini Enterprise license sync: {status}",
        "",
        f"When:         started {started}, completed {completed} ({duration}s)",
        f"Why:          {why}",
        f"Subscription: {subscription}",
        f"Groups:       {run_record.get('monitored_groups_count', len(groups))} monitored",
    ]
    for g in groups:
        lines.append(f"             - {g}")
    lines += [
        "",
        "What happened:",
        f"  Users evaluated:        {evaluated}",
        f"  Licenses assigned:      {assigned}",
        f"  Already held (skipped): {held}",
        f"  Nested groups skipped:  {nested}",
        f"  Errors:                 {err_count}",
    ]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for e in errors:
            lines.append(
                f"  [{e.get('type', 'ERROR')}] {e.get('item', '')}: {e.get('error', '')}"
            )
    lines += ["", f"Run history: {history_url}"]
    if doc_id:
        lines.append(f"Run id: {doc_id}")
    text = "\n".join(lines)

    # ---- html ----
    color = {"SUCCESS": "#16a34a", "PARTIAL_SUCCESS": "#d97706", "FAILED": "#dc2626"}.get(status, "#334155")

    def esc(v: Any) -> str:
        return html.escape(str(v))

    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#64748b'>{esc(k)}</td>"
        f"<td style='padding:4px 0;font-weight:600'>{esc(v)}</td></tr>"
        for k, v in [
            ("Users evaluated", evaluated),
            ("Licenses assigned", assigned),
            ("Already held (skipped)", held),
            ("Nested groups skipped", nested),
            ("Errors", err_count),
        ]
    )
    groups_html = "".join(f"<li>{esc(g)}</li>" for g in groups) or "<li><em>none</em></li>"
    errors_html = ""
    if errors:
        items = "".join(
            f"<li><code>{esc(e.get('type', 'ERROR'))}</code> "
            f"<strong>{esc(e.get('item', ''))}</strong>: {esc(e.get('error', ''))}</li>"
            for e in errors
        )
        errors_html = (
            f"<h3 style='margin:16px 0 4px'>Errors ({esc(err_count)})</h3>"
            f"<ul style='margin:0;padding-left:18px;color:#b91c1c'>{items}</ul>"
        )

    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#0f172a;max-width:640px">
  <p style="font-size:16px;margin:0 0 12px">
    Gemini Enterprise license sync:
    <strong style="color:{color}">{esc(status)}</strong>
  </p>
  <table style="border-collapse:collapse;margin-bottom:8px">
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">When</td>
        <td style="padding:4px 0">started {esc(started)}<br>completed {esc(completed)} ({esc(duration)}s)</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Why</td>
        <td style="padding:4px 0">{esc(why)}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Subscription</td>
        <td style="padding:4px 0">{esc(subscription)}</td></tr>
  </table>
  <h3 style="margin:16px 0 4px">Monitored groups</h3>
  <ul style="margin:0;padding-left:18px">{groups_html}</ul>
  <h3 style="margin:16px 0 4px">What happened</h3>
  <table style="border-collapse:collapse">{rows}</table>
  {errors_html}
  <p style="margin:20px 0 0">
    <a href="{esc(history_url)}"
       style="background:#2563eb;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none">
      Open run history
    </a>
  </p>
  {f'<p style="color:#94a3b8;font-size:12px;margin:8px 0 0">Run id: {esc(doc_id)}</p>' if doc_id else ''}
</div>"""

    return {"subject": subject, "text": text, "html": html_body}


def send_sync_notification(config: Dict[str, Any], run_record: Dict[str, Any]) -> Dict[str, Any]:
    """Send the run notification if the saved preference calls for it.

    Returns a small result dict; never raises.
    """
    recipients = _recipients(config)
    if not should_notify(config, run_record):
        return {"sent": False, "reason": "not required by notify_on preference or no recipients"}

    sender = (
        settings.NOTIFICATION_SENDER_EMAIL
        or config.get("delegated_admin_email")
        or settings.DELEGATED_ADMIN_EMAIL
    )
    parts = build_message(config, run_record)

    mime = MIMEMultipart("alternative")
    mime["To"] = ", ".join(recipients)
    mime["From"] = sender
    mime["Subject"] = parts["subject"]
    mime.attach(MIMEText(parts["text"], "plain", "utf-8"))
    mime.attach(MIMEText(parts["html"], "html", "utf-8"))
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    try:
        service = WorkspaceClient().get_gmail_service(subject_email=sender)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info("Sent sync notification to %s (status %s)", recipients, run_record.get("status"))
        return {"sent": True, "recipients": recipients}
    except Exception as e:  # noqa: BLE001 - a mail failure must not fail the sync
        logger.error("Failed to send sync notification to %s: %s", recipients, e)
        return {"sent": False, "reason": str(e), "recipients": recipients}
