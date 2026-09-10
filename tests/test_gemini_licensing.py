import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import gemini_licensing as gl


def test_config_name_helpers():
    good = "projects/750/locations/us/licenseConfigs/gemini_ent"
    assert gl.is_valid_config_name(good)
    assert gl.location_from_config_name(good) == "us"
    assert not gl.is_valid_config_name("Google-Apps")
    assert not gl.is_valid_config_name("projects/750/locations/mars/licenseConfigs/x")
    assert gl.location_from_config_name("nonsense") is None


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _client(get_map=None, post_payload=None):
    """A GeminiLicenseClient whose session returns canned responses by URL substring."""
    get_map = get_map or {}
    sess = MagicMock()

    def _get(url, headers=None, params=None, timeout=None):
        for frag, payload in get_map.items():
            if frag in url:
                return _Resp(payload)
        return _Resp({})

    sess.get.side_effect = _get
    sess.post.side_effect = lambda url, headers=None, json=None, timeout=None: _Resp(post_payload or {})
    with patch("app.gemini_licensing.google.auth.default", return_value=(MagicMock(), "proj")), \
         patch("app.gemini_licensing.AuthorizedSession", return_value=sess):
        c = gl.GeminiLicenseClient(project_id="acme")
        c._session = sess
        return c, sess


def test_list_license_configs_merges_locations_and_labels():
    us_cfg = {
        "licenseConfigs": [{
            "name": "projects/1/locations/us/licenseConfigs/free_trial_gemini",
            "licenseCount": "50", "state": "ACTIVE", "freeTrial": True,
            "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
        }]
    }
    c, _ = _client(get_map={"/locations/us/licenseConfigs": us_cfg,
                            "/locations/global/licenseConfigs": {},
                            "/locations/eu/licenseConfigs": {}})
    out = c.list_license_configs()
    assert len(out) == 1
    cfg = out[0]
    assert cfg["_location"] == "us"
    assert "Gemini Enterprise (Search + Assistant)" in cfg["label"]
    assert "50 seats" in cfg["label"] and "us" in cfg["label"] and "Free trial" in cfg["label"]
    assert "[ACTIVE]" in cfg["label"]


def test_assigned_user_emails_filters_and_lowercases():
    page = {"userLicenses": [
        {"userPrincipal": "Alice@Example.com", "licenseAssignmentState": "ASSIGNED"},
        {"userPrincipal": "bob@example.com", "licenseAssignmentState": "NO_LICENSE"},
        {"userPrincipal": "carol@example.com", "licenseAssignmentState": "ASSIGNED"},
    ]}
    c, _ = _client(get_map={"/userLicenses": page})
    assert c.assigned_user_emails("us") == {"alice@example.com", "carol@example.com"}


def test_batch_assign_builds_additive_request_and_reads_counts():
    done_op = {"name": "op/1", "done": True, "metadata": {"successCount": 2, "failureCount": 0}}
    c, sess = _client(post_payload=done_op)
    res = c.batch_assign("projects/1/locations/us/licenseConfigs/gc", ["a@x.com", "b@x.com"])
    assert res["assigned"] == 2 and res["failed"] == 0
    _, kwargs = sess.post.call_args
    body = kwargs["json"]
    assert body["deleteUnassignedUserLicenses"] is False
    assert body["inlineSource"]["userLicenses"][0] == {
        "userPrincipal": "a@x.com",
        "licenseConfig": "projects/1/locations/us/licenseConfigs/gc",
    }
    assert "batchUpdateUserLicenses" in sess.post.call_args[0][0]


def test_batch_assign_polls_until_done():
    c, sess = _client(
        get_map={"op/xyz": {"name": "op/xyz", "done": True,
                            "response": {"successCount": 1, "errorSamples": []}}},
        post_payload={"name": "op/xyz", "done": False},
    )
    res = c.batch_assign("projects/1/locations/eu/licenseConfigs/gc", ["a@x.com"], poll_timeout=5)
    assert res["assigned"] == 1
    assert sess.get.called


def test_batch_assign_no_emails_is_noop():
    c, sess = _client()
    res = c.batch_assign("projects/1/locations/us/licenseConfigs/gc", [])
    assert res == {"assigned": 0, "failed": 0, "errors": [], "operation": None}
    sess.post.assert_not_called()


def test_batch_assign_rejects_bad_config_name():
    c, _ = _client()
    with pytest.raises(ValueError):
        c.batch_assign("Google-Apps", ["a@x.com"])
