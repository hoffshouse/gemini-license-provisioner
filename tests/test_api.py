import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings

client = TestClient(app)


def test_iap_enforced_blocks_unauthenticated(monkeypatch):
    """With IAP_AUDIENCE set, pages and mutating APIs require an IAP assertion."""
    monkeypatch.setattr(settings, "IAP_AUDIENCE", "test-aud")
    assert client.get("/").status_code == 401
    assert client.post("/api/settings", json={
        "delegated_admin_email": "a@b.com", "license_config": ""
    }).status_code == 401
    # health probe stays open for Cloud Run
    assert client.get("/healthz").status_code == 200


def test_iap_enforced_allows_super_admin(monkeypatch):
    monkeypatch.setattr(settings, "IAP_AUDIENCE", "test-aud")
    monkeypatch.setattr("app.auth._verify_iap_assertion", lambda a: {"email": "boss@example.com"})
    monkeypatch.setattr("app.auth.is_super_admin", lambda e: True)
    mock_config = {
        "monitored_groups": [], "license_config": "", "license_label": "",
        "delegated_admin_email": "admin@example.com", "cron_expression": "0 2 * * *",
        "notification_emails": [], "notify_on": "failures",
    }
    with patch("app.main.get_config", return_value=mock_config), \
         patch("app.main.get_sync_history", return_value=[]):
        r = client.get("/", headers={"x-goog-iap-jwt-assertion": "tok"})
        assert r.status_code == 200
        assert "boss@example.com" in r.text


def test_healthz():
    """Verify healthcheck probe returns 200."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_dashboard_route():
    """Verify dashboard renders HTML."""
    mock_config = {
        "monitored_groups": ["team@example.com"],
        "license_config": "",
        "license_label": "",
        "delegated_admin_email": "admin@example.com",
        "cron_expression": "0 2 * * *"
    }
    with patch("app.main.get_config", return_value=mock_config), \
         patch("app.main.get_sync_history", return_value=[]):
        response = client.get("/")
        assert response.status_code == 200
        assert "Gemini License Provisioning Dashboard" in response.text
        assert "team@example.com" in response.text


def test_api_save_groups():
    """Verify saving monitored groups via API."""
    with patch("app.main.update_config", return_value={"monitored_groups": ["group1@domain.com"]}):
        response = client.post(
            "/api/groups",
            json={"groups": ["group1@domain.com"]}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["monitored_groups"] == ["group1@domain.com"]


_LC = "projects/750/locations/us/licenseConfigs/gemini_ent"


def test_api_save_settings_with_valid_subscription():
    gem = MagicMock()
    gem.list_license_configs.return_value = [{"name": _LC, "label": "Gemini Enterprise — us"}]
    with patch("app.main.update_config", return_value={}), \
         patch("app.main.GeminiLicenseClient", return_value=gem):
        response = client.post("/api/settings", json={
            "delegated_admin_email": "admin@test.com", "license_config": _LC,
        })
        assert response.status_code == 200
        assert response.json()["success"] is True


def test_api_save_settings_rejects_workspace_sku():
    with patch("app.main.update_config", return_value={}):
        response = client.post("/api/settings", json={
            "delegated_admin_email": "admin@test.com", "license_config": "Google-Apps",
        })
        assert response.status_code == 400


def test_api_save_settings_rejects_unknown_subscription():
    gem = MagicMock()
    gem.list_license_configs.return_value = [{"name": _LC, "label": "x"}]
    with patch("app.main.update_config", return_value={}), \
         patch("app.main.GeminiLicenseClient", return_value=gem):
        response = client.post("/api/settings", json={
            "delegated_admin_email": "admin@test.com",
            "license_config": "projects/750/locations/us/licenseConfigs/other",
        })
        assert response.status_code == 400


def test_settings_view_renders_subscription_dropdown():
    mock_config = {
        "monitored_groups": [], "license_config": _LC, "license_label": "Gemini Enterprise — us",
        "delegated_admin_email": "admin@example.com", "cron_expression": "0 2 * * *",
    }
    gem = MagicMock()
    gem.list_license_configs.return_value = [
        {"name": _LC, "label": "Gemini Enterprise — 50 seats — us — Free trial [ACTIVE]"},
    ]
    with patch("app.main.get_config", return_value=mock_config), \
         patch("app.main.GeminiLicenseClient", return_value=gem):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Gemini Enterprise License Subscription" in r.text
        assert "Free trial [ACTIVE]" in r.text
        assert "Product ID" not in r.text and "SKU ID" not in r.text


def test_settings_view_handles_license_api_error():
    mock_config = {"monitored_groups": [], "license_config": "", "license_label": "",
                   "delegated_admin_email": "a@e.com", "cron_expression": "0 2 * * *"}
    gem = MagicMock()
    gem.list_license_configs.side_effect = RuntimeError("permission denied")
    with patch("app.main.get_config", return_value=mock_config), \
         patch("app.main.GeminiLicenseClient", return_value=gem):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Could not list license subscriptions" in r.text


def test_schedule_view_renders_notification_settings():
    """The Sync Schedule page shows saved notification recipients + mode."""
    mock_config = {
        "monitored_groups": [],
        "license_config": "",
        "license_label": "",
        "delegated_admin_email": "admin@example.com",
        "cron_expression": "0 2 * * *",
        "notification_emails": ["ops@example.com"],
        "notify_on": "all",
    }
    sched_status = {"available": False, "job_name": "job", "schedule": None, "time_zone": "UTC"}
    with patch("app.main.get_config", return_value=mock_config), \
         patch("app.main.SchedulerService") as MockSched:
        MockSched.return_value.get_schedule.return_value = sched_status
        response = client.get("/schedule")
        assert response.status_code == 200
        assert "Run Notifications" in response.text
        assert "ops@example.com" in response.text
        assert "checkbox" in response.text and "notify-all" in response.text


def test_api_save_notifications_valid():
    """Save notification recipients + mode via API."""
    with patch("app.main.update_config", return_value={
        "notification_emails": ["ops@example.com", "sre@example.com"],
        "notify_on": "all",
    }):
        response = client.post(
            "/api/notifications",
            json={"notification_emails": "ops@example.com, sre@example.com", "notify_on": "all"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["notify_on"] == "all"
        assert "ops@example.com" in data["notification_emails"]


def test_api_save_notifications_rejects_bad_email():
    response = client.post(
        "/api/notifications",
        json={"notification_emails": ["not-an-email"], "notify_on": "failures"},
    )
    assert response.status_code == 400


def test_api_save_notifications_rejects_bad_mode():
    response = client.post(
        "/api/notifications",
        json={"notification_emails": [], "notify_on": "sometimes"},
    )
    assert response.status_code == 400


def test_api_test_connection():
    """Verify DWD test connection endpoint returns result structure."""
    mock_res = {
        "success": True,
        "message": "Connected",
        "subject": "admin@test.com"
    }
    with patch("app.main.WorkspaceClient") as MockClient:
        mock_instance = MagicMock()
        mock_instance.test_dwd_connection.return_value = mock_res
        MockClient.return_value = mock_instance

        response = client.post(
            "/api/test-connection",
            json={"delegated_admin_email": "admin@test.com"}
        )
        assert response.status_code == 200
        assert response.json()["success"] is True
