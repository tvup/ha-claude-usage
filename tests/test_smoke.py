"""Smoke tests for the Claude Usage integration.

Coverage:
  - validate_credentials: success / auth failure / Cloudflare block / network error
  - ClaudeUsageCoordinator._async_update_data: success path + metrics update
  - ClaudeUsageCoordinator._async_update_data: auth failure increments failed_requests
  - ClaudeUsageSensor.native_value: usage sensor reads from coordinator.data
  - ClaudeUsageSensor.available: usage sensor unavailable when data is None
  - __init__._mask / _success_rate helpers
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.claude_usage.__init__ import _mask, _success_rate
from custom_components.claude_usage.const import (
    BASE_URL,
    DOMAIN,
    OPTIONAL_ENDPOINTS,
    USAGE_ENDPOINT,
)
from custom_components.claude_usage.coordinator import (
    ClaudeUsageCoordinator,
    UsageMetrics,
    _transform,
    validate_credentials,
)
from custom_components.claude_usage.sensor import (
    SENSORS,
    ClaudeUsageSensor,
)

from .conftest import CF_CLEARANCE, ORG_ID, SESSION_KEY

_USAGE_URL = BASE_URL + USAGE_ENDPOINT.format(org_id=ORG_ID)

# ── validate_credentials ────────────────────────────────────────────────────


async def test_validate_credentials_success(fake_raw_api_data):
    """HTTP 200 → returns parsed JSON without raising."""
    with aioresponses() as m:
        m.get(_USAGE_URL, payload=fake_raw_api_data, status=200)
        result, _cookies = await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)

    assert result["session_5h"]["utilization"] == 42


async def test_validate_credentials_session_expired():
    """HTTP 401 without Cloudflare body → ConfigEntryAuthFailed('session_expired')."""
    with aioresponses() as m:
        m.get(_USAGE_URL, body="Unauthorized", status=401)
        with pytest.raises(ConfigEntryAuthFailed, match="session_expired"):
            await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)


async def test_validate_credentials_cloudflare_block():
    """HTTP 403 with Cloudflare body → ConfigEntryAuthFailed('cf_clearance_expired')."""
    with aioresponses() as m:
        m.get(_USAGE_URL, body="Just a moment... cloudflare", status=403)
        with pytest.raises(ConfigEntryAuthFailed, match="cf_clearance_expired"):
            await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)


async def test_validate_credentials_302_redirect():
    """HTTP 302 → ConfigEntryAuthFailed('session_expired')."""
    with aioresponses() as m:
        m.get(_USAGE_URL, status=302, headers={"Location": "https://claude.ai/login"})
        with pytest.raises(ConfigEntryAuthFailed, match="session_expired"):
            await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)


async def test_validate_credentials_unexpected_status():
    """HTTP 500 → UpdateFailed."""
    with aioresponses() as m:
        m.get(_USAGE_URL, status=500, body="Internal Server Error")
        with pytest.raises(UpdateFailed, match="500"):
            await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)


# ── ClaudeUsageCoordinator ──────────────────────────────────────────────────


async def test_coordinator_success_updates_metrics(
    hass: HomeAssistant, mock_entry, fake_raw_api_data
):
    """Successful fetch updates data and increments total_requests / last_response_ms."""
    mock_entry.add_to_hass(hass)
    coordinator = ClaudeUsageCoordinator(hass, mock_entry)

    with aioresponses() as m:
        m.get(_USAGE_URL, payload=fake_raw_api_data, status=200)
        data = await coordinator._async_update_data()

    assert data["session_5h"]["utilization"] == 42
    assert coordinator.metrics.total_requests == 1
    assert coordinator.metrics.failed_requests == 0
    assert coordinator.metrics.last_response_ms is not None
    assert coordinator.metrics.last_success_at is not None


async def test_coordinator_auth_failure_increments_failed(
    hass: HomeAssistant, mock_entry
):
    """Auth failure increments failed_requests and re-raises ConfigEntryAuthFailed."""
    mock_entry.add_to_hass(hass)
    coordinator = ClaudeUsageCoordinator(hass, mock_entry)

    with aioresponses() as m:
        m.get(_USAGE_URL, body="Unauthorized", status=401)
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    assert coordinator.metrics.total_requests == 1
    assert coordinator.metrics.failed_requests == 1


async def test_coordinator_cookie_renewal(
    hass: HomeAssistant, mock_entry, fake_raw_api_data
):
    """When server sends a new cf_clearance cookie, cookie_renewals is incremented."""
    mock_entry.add_to_hass(hass)
    coordinator = ClaudeUsageCoordinator(hass, mock_entry)

    with aioresponses() as m:
        m.get(
            _USAGE_URL,
            payload=fake_raw_api_data,
            status=200,
            headers={"Set-Cookie": "cf_clearance=new-clearance-value; Path=/"},
        )
        await coordinator._async_update_data()

    assert coordinator.metrics.cookie_renewals == 1


# ── UsageMetrics defaults ────────────────────────────────────────────────────


def test_usage_metrics_defaults():
    m = UsageMetrics()
    assert m.total_requests == 0
    assert m.failed_requests == 0
    assert m.cookie_renewals == 0
    assert m.last_success_at is None
    assert m.last_response_ms is None


# ── Sensor native_value ──────────────────────────────────────────────────────


def _make_sensor(coordinator, description):
    """Instantiate a ClaudeUsageSensor without going through HA entity registry."""
    sensor = ClaudeUsageSensor.__new__(ClaudeUsageSensor)
    sensor.coordinator = coordinator
    sensor.entity_description = description
    return sensor


def test_usage_sensor_reads_value_fn(fake_usage_data):
    """Usage sensor extracts the correct value from coordinator.data."""
    coordinator = MagicMock()
    coordinator.data = fake_usage_data

    session_desc = next(s for s in SENSORS if s.key == "session_utilization")
    sensor = _make_sensor(coordinator, session_desc)
    assert sensor.native_value == 42


def test_usage_sensor_none_when_data_missing():
    """Usage sensor returns None when coordinator.data is None."""
    coordinator = MagicMock()
    coordinator.data = None

    session_desc = next(s for s in SENSORS if s.key == "session_utilization")
    sensor = _make_sensor(coordinator, session_desc)
    assert sensor.native_value is None


# ── Sensor available ─────────────────────────────────────────────────────────


def test_usage_sensor_unavailable_when_no_data():
    """Usage sensor.available is False when coordinator.data is None."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = None

    session_desc = next(s for s in SENSORS if s.key == "session_utilization")
    sensor = ClaudeUsageSensor.__new__(ClaudeUsageSensor)
    sensor.coordinator = coordinator
    sensor.entity_description = session_desc
    # Bypass CoordinatorEntity.available by calling the property directly
    assert sensor.available is False


# ── Helper functions ─────────────────────────────────────────────────────────


def test_mask_short_value():
    assert _mask("abc") == "abc***"


def test_mask_long_value():
    result = _mask("sk-ant-session-supersecrettoken")
    assert result == "sk-ant-ses***"
    assert "supersecrettoken" not in result


def test_mask_empty():
    assert _mask("") == ""


def test_success_rate_normal():
    assert _success_rate(100, 10) == 90.0


def test_success_rate_zero_total():
    assert _success_rate(0, 0) is None


def test_success_rate_all_failed():
    assert _success_rate(5, 5) == 0.0


# ── Endpoints optionnels + nouveaux champs ──────────────────────────────────

_ORG_URL = BASE_URL + OPTIONAL_ENDPOINTS["organization"].format(org_id=ORG_ID)
_PREPAID_URL = BASE_URL + OPTIONAL_ENDPOINTS["prepaid_credits"].format(org_id=ORG_ID)


async def test_fetch_includes_optional_endpoints(
    fake_raw_api_data, fake_organization_data, fake_prepaid_credits_data
):
    """Optional endpoints feed plan, usage credits and the raw payloads."""
    with aioresponses() as m:
        m.get(_USAGE_URL, payload=fake_raw_api_data, status=200)
        m.get(_ORG_URL, payload=fake_organization_data, status=200)
        m.get(_PREPAID_URL, payload=fake_prepaid_credits_data, status=200)
        result, _cookies = await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)

    assert result["subscription_type"] == "Max 5x"
    assert result["usage_credits"] == {"amount": 12.5, "amount_cents": 1250, "currency": "USD"}
    assert set(result["raw"]) == {"usage", "organization", "prepaid_credits"}


async def test_optional_endpoint_failure_is_ignored(fake_raw_api_data):
    """A failing optional endpoint never fails the update."""
    with aioresponses() as m:
        m.get(_USAGE_URL, payload=fake_raw_api_data, status=200)
        m.get(_ORG_URL, status=404, body="Not found")
        m.get(_PREPAID_URL, status=500, body="boom")
        result, _cookies = await validate_credentials(SESSION_KEY, CF_CLEARANCE, ORG_ID)

    assert result["session_5h"]["utilization"] == 42
    assert result["subscription_type"] is None
    assert result["usage_credits"]["amount"] is None
    assert set(result["raw"]) == {"usage"}


def test_transform_subscription_from_capabilities(fake_raw_api_data):
    data = _transform(fake_raw_api_data, {"organization": {"capabilities": ["chat", "claude_pro"]}})
    assert data["subscription_type"] == "Pro"


def test_transform_cloud_credits_and_limit_resets(fake_raw_api_data):
    raw = {
        **fake_raw_api_data,
        "cloud_session_credits": {
            "remaining_cents": 1000,
            "total_cents": 10000,
            "expires_at": "2026-11-05T07:59:00+00:00",
            "currency": "USD",
        },
        "limit_resets": [
            {"expires_at": "2026-11-20T00:00:00+00:00"},
            {"expires_at": "2026-11-01T00:00:00+00:00"},
        ],
    }
    data = _transform(raw)

    assert data["cloud_credits"] == {
        "remaining": 10.0,
        "total": 100.0,
        "expires_at": "2026-11-05T07:59:00+00:00",
        "currency": "USD",
    }
    assert data["limit_resets"] == {"count": 2, "next_expires_at": "2026-11-01T00:00:00+00:00"}


def test_transform_unknown_sections_are_none(fake_raw_api_data):
    data = _transform(fake_raw_api_data)
    assert data["cloud_credits"]["remaining"] is None
    assert data["limit_resets"]["count"] is None


def test_new_sensors_read_values(fake_usage_data):
    coordinator = MagicMock()
    coordinator.data = fake_usage_data

    def value(key):
        return _make_sensor(coordinator, next(s for s in SENSORS if s.key == key)).native_value

    assert value("subscription_type") == "Pro"
    assert value("limit_resets_count") == 1
    assert value("cloud_credits_left") == 10.0
    assert value("cloud_credits_total") == 100.0
    assert value("cloud_credits_expires_at").isoformat() == "2026-11-05T07:59:00+00:00"
    assert value("usage_credits") == 12.5
    assert value("usage_credits_enabled") == "on"
    assert value("monthly_spend_limit") == 40.0
    assert value("monthly_spend_used") == 0.1


def test_usage_sensor_exposes_resets_at(fake_usage_data):
    coordinator = MagicMock()
    coordinator.data = fake_usage_data

    session_desc = next(s for s in SENSORS if s.key == "session_utilization")
    sensor = _make_sensor(coordinator, session_desc)
    assert sensor.extra_state_attributes == {"resets_at": "2026-04-14T05:00:00+00:00"}


def test_transform_codenamed_sections(fake_raw_api_data):
    raw = {
        **fake_raw_api_data,
        "extra_usage": None,
        "iguana_necktie": {
            "utilization": 89.707163,
            "resets_at": "2026-11-05T07:59:00+00:00",
            "limit_dollars": 100,
            "used_dollars": 89.707163,
            "remaining_dollars": 10.292837000000006,
        },
        "cedar_ember": {
            "grants": [
                {"resets_left": 0, "ends_at": "2026-10-22T16:00:00+00:00"},
                {"resets_left": 2, "ends_at": "2026-11-20T00:00:00+00:00"},
            ],
        },
    }
    data = _transform(raw)

    assert data["cloud_credits"] == {
        "remaining": 10.29,
        "total": 100.0,
        "expires_at": "2026-11-05T07:59:00+00:00",
        "currency": "USD",
    }
    assert data["limit_resets"] == {"count": 2, "next_expires_at": "2026-11-20T00:00:00+00:00"}
    assert data["extra_usage"]["used"] is None
    assert data["extra_usage"]["limit"] is None
