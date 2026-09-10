#!/usr/bin/env python3
"""Dependency-free smoke test of the sync engine.

Mocks every cloud dependency (no pip install required) and checks:
1. Direct members are processed; nested groups are flagged, not licensed.
2. Users who already hold a license from the subscription are skipped.
3. Only not-yet-licensed users are sent to batchUpdateUserLicenses.
4. Deduplication across multiple groups.
5. A Workspace product/SKU value is rejected (Gemini Enterprise only).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import MagicMock, patch

for mod in [
    "pydantic", "pydantic_settings", "google", "google.cloud", "google.cloud.firestore",
    "google.cloud.scheduler_v1", "google.oauth2", "google.oauth2.service_account",
    "google.auth", "google.auth.iam", "google.auth.transport", "google.auth.transport.requests",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
    "fastapi", "fastapi.staticfiles", "fastapi.templating", "jinja2",
]:
    sys.modules.setdefault(mod, MagicMock())


class MockBaseSettings:
    def __init__(self, **kwargs):
        for k, v in self.__class__.__dict__.items():
            if not k.startswith("_") and not isinstance(v, (property, staticmethod, classmethod)):
                setattr(self, k, v)


sys.modules["pydantic_settings"].BaseSettings = MockBaseSettings
sys.modules["pydantic_settings"].SettingsConfigDict = lambda **kwargs: None

from app.sync_worker import run_license_sync  # noqa: E402

_CFG = "projects/750/locations/us/licenseConfigs/gemini_ent"


def _cfg(**over):
    base = {
        "monitored_groups": ["ai-engineers@example.com"],
        "license_config": _CFG,
        "license_label": "Gemini Enterprise — us",
        "delegated_admin_email": "admin@example.com",
    }
    base.update(over)
    return base


def _run(config, members_map, already_licensed, batch=None):
    ws = MagicMock()
    ws.list_direct_group_members.side_effect = lambda g: members_map.get(g, [])
    gem = MagicMock()
    gem.assigned_user_emails.return_value = set(already_licensed)
    gem.batch_assign.return_value = batch or {"assigned": 0, "failed": 0, "errors": [], "operation": None}
    with patch("app.sync_worker.get_config", return_value=config), \
         patch("app.sync_worker.record_sync_history", return_value="doc1"), \
         patch("app.sync_worker.send_sync_notification", return_value={"sent": False}), \
         patch("app.sync_worker.WorkspaceClient", return_value=ws), \
         patch("app.sync_worker.GeminiLicenseClient", return_value=gem):
        return run_license_sync(triggered_by="local_test"), gem


def run_tests():
    print("=" * 67)
    print(" Local smoke test: Gemini Enterprise license sync engine")
    print("=" * 67)

    print("\n[1] Nested groups flagged; already-licensed users skipped...")
    members = {"ai-engineers@example.com": [
        {"email": "user1@example.com", "type": "USER"},
        {"email": "user2@example.com", "type": "USER"},
        {"email": "subgroup@example.com", "type": "GROUP"},
    ]}
    result, gem = _run(_cfg(), members, already_licensed=["user2@example.com"],
                       batch={"assigned": 1, "failed": 0, "errors": [], "operation": "op/1"})
    assert result["status"] == "PARTIAL_SUCCESS", result["status"]
    assert result["evaluated_users_count"] == 2
    assert result["licenses_already_held_count"] == 1
    assert result["licenses_assigned_count"] == 1
    assert result["nested_groups_count"] == 1
    assert "Nested groups are not supported" in result["errors"][0]["error"]
    gem.batch_assign.assert_called_once_with(_CFG, ["user1@example.com"])
    print("  ✓ nested group logged; user2 skipped; only user1 assigned")

    print("\n[2] Dedupe across groups; nobody licensed yet...")
    members = {
        "group-a@example.com": [{"email": "charlie@example.com", "type": "USER"},
                                {"email": "dan@example.com", "type": "USER"}],
        "group-b@example.com": [{"email": "charlie@example.com", "type": "USER"},
                                {"email": "eve@example.com", "type": "USER"}],
    }
    result, gem = _run(_cfg(monitored_groups=list(members)), members, already_licensed=[],
                       batch={"assigned": 3, "failed": 0, "errors": [], "operation": "op/2"})
    assert result["evaluated_users_count"] == 3, result["evaluated_users_count"]
    assert result["licenses_assigned_count"] == 3
    assert result["status"] == "SUCCESS"
    assert gem.batch_assign.call_args[0][1] == ["charlie@example.com", "dan@example.com", "eve@example.com"]
    print("  ✓ charlie deduplicated; 3 users assigned in one batch")

    print("\n[3] No groups configured...")
    result, gem = _run(_cfg(monitored_groups=[]), {}, already_licensed=[])
    assert result["status"] == "SUCCESS" and result["evaluated_users_count"] == 0
    gem.batch_assign.assert_not_called()
    print("  ✓ handled gracefully")

    print("\n[4] Workspace product/SKU rejected...")
    result, gem = _run(_cfg(license_config="Google-Apps"), {}, already_licensed=[])
    assert result["status"] == "FAILED"
    assert result["errors"][0]["type"] == "INVALID_LICENSE_CONFIG"
    gem.assigned_user_emails.assert_not_called()
    print("  ✓ only Gemini Enterprise license configs are accepted")

    print("\n" + "=" * 67)
    print(" ALL LOCAL SMOKE TESTS PASSED (4/4)")
    print("=" * 67)


if __name__ == "__main__":
    run_tests()
