import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.sync_worker import run_license_sync

_CFG = "projects/750/locations/us/licenseConfigs/gemini_ent"


def _config(**over):
    base = {
        "monitored_groups": ["ai-team@example.com"],
        "license_config": _CFG,
        "license_label": "Gemini Enterprise — us",
        "delegated_admin_email": "admin@example.com",
    }
    base.update(over)
    return base


def _run(config, members=None, already_licensed=None, batch_result=None):
    ws = MagicMock()
    ws.list_direct_group_members.return_value = members or []
    gem = MagicMock()
    gem.assigned_user_emails.return_value = set(already_licensed or [])
    gem.batch_assign.return_value = batch_result or {"assigned": 0, "failed": 0, "errors": [], "operation": None}
    with patch("app.sync_worker.get_config", return_value=config), \
         patch("app.sync_worker.record_sync_history", return_value="doc1"), \
         patch("app.sync_worker.send_sync_notification", return_value={"sent": False}), \
         patch("app.sync_worker.WorkspaceClient", return_value=ws), \
         patch("app.sync_worker.GeminiLicenseClient", return_value=gem):
        return run_license_sync(triggered_by="test"), ws, gem


def test_skips_users_who_already_have_a_license():
    members = [
        {"email": "alice@example.com", "type": "USER"},
        {"email": "bob@example.com", "type": "USER"},
        {"email": "subteam@example.com", "type": "GROUP"},
    ]
    result, ws, gem = _run(
        _config(), members=members,
        already_licensed=["bob@example.com"],
        batch_result={"assigned": 1, "failed": 0, "errors": [], "operation": "op/1"},
    )
    assert result["status"] == "PARTIAL_SUCCESS"          # 1 nested-group error
    assert result["evaluated_users_count"] == 2
    assert result["licenses_already_held_count"] == 1     # bob, skipped
    assert result["licenses_assigned_count"] == 1         # alice
    assert result["nested_groups_count"] == 1
    # only the not-yet-licensed user is sent to batch_assign
    gem.batch_assign.assert_called_once_with(_CFG, ["alice@example.com"])


def test_no_assign_call_when_everyone_is_already_licensed():
    members = [{"email": "alice@example.com", "type": "USER"}]
    result, ws, gem = _run(_config(), members=members, already_licensed=["alice@example.com"])
    assert result["status"] == "SUCCESS"
    assert result["licenses_already_held_count"] == 1
    assert result["licenses_assigned_count"] == 0
    gem.batch_assign.assert_not_called()


def test_rejects_workspace_product_sku_as_license_config():
    result, ws, gem = _run(_config(license_config="Google-Apps"))
    assert result["status"] == "FAILED"
    assert result["errors"][0]["type"] == "INVALID_LICENSE_CONFIG"
    gem.assigned_user_emails.assert_not_called()


def test_fails_when_no_subscription_selected():
    result, ws, gem = _run(_config(license_config="", license_label=""))
    assert result["status"] == "FAILED"
    assert result["errors"][0]["type"] == "NO_LICENSE_CONFIG"


def test_empty_groups_is_success():
    result, ws, gem = _run(_config(monitored_groups=[]))
    assert result["status"] == "SUCCESS"
    assert result["evaluated_users_count"] == 0
    assert "No groups configured" in result["message"]


def test_batch_assign_failure_is_recorded():
    members = [{"email": "alice@example.com", "type": "USER"}]
    result, ws, gem = _run(
        _config(), members=members, already_licensed=[],
        batch_result={"assigned": 0, "failed": 1, "errors": ["quota exceeded"], "operation": "op/2"},
    )
    assert result["status"] == "FAILED"
    assert any(e["type"] == "LICENSE_ASSIGN_FAILED" and "quota" in e["error"] for e in result["errors"])
