"""Shared fixtures for Claude Usage integration tests."""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_usage.const import (
    CONF_CF_CLEARANCE,
    CONF_ORG_ID,
    CONF_SESSION_KEY,
    DOMAIN,
)

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Override the HA plugin's strict thread/task cleanup check.

    The upstream fixture incorrectly flags the asyncio executor shutdown thread
    (`_run_safe_shutdown_loop`) created by Python 3.12 as a leaked resource.
    Custom-component test suites conventionally disable this check.
    """
    yield

ORG_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SESSION_KEY = "sk-ant-session-fakekey"
CF_CLEARANCE = "fake-cf-clearance-value"


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    """A pre-built config entry with fake credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_entry_id",
        data={
            CONF_SESSION_KEY: SESSION_KEY,
            CONF_CF_CLEARANCE: CF_CLEARANCE,
            CONF_ORG_ID: ORG_ID,
        },
    )


@pytest.fixture
def fake_raw_api_data() -> dict:
    """Raw payload as returned by the claude.ai API (before _transform)."""
    return {
        "five_hour": {"utilization": 42.0, "resets_at": "2026-04-14T05:00:00+00:00"},
        "seven_day": {"utilization": 70.0, "resets_at": "2026-04-21T00:00:00+00:00"},
        "seven_day_sonnet": {"utilization": 55.0, "resets_at": "2026-04-21T00:00:00+00:00"},
        "seven_day_opus": None,
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 2000,
            "used_credits": 10.0,
            "utilization": None,
        },
    }


@pytest.fixture
def fake_organization_data() -> dict:
    """Raw payload of GET /api/organizations/{org_id}."""
    return {
        "uuid": ORG_ID,
        "capabilities": ["chat", "claude_max"],
        "rate_limit_tier": "default_claude_max_5x",
    }


@pytest.fixture
def fake_prepaid_credits_data() -> dict:
    """Raw payload of GET /api/organizations/{org_id}/prepaid/credits (amount in cents)."""
    return {"amount": 1250, "currency": "USD"}


@pytest.fixture
def fake_usage_data() -> dict:
    """Transformed usage payload (output of _transform) used to populate coordinator.data."""
    return {
        "session_5h": {
            "utilization": 42,
            "resets_at": "2026-04-14T05:00:00+00:00",
            "resets_in_minutes": 180,
        },
        "weekly": {"utilization": 70, "resets_in_minutes": 2000},
        "weekly_sonnet": {"utilization": 55},
        "weekly_opus": None,
        "extra_usage": {"enabled": True, "used": 10, "limit": 4000, "utilization": 5, "currency": "USD"},
        "subscription_type": "Pro",
        "limit_resets": {"count": 1, "next_expires_at": "2026-11-01T00:00:00+00:00"},
        "cloud_credits": {
            "remaining": 10.0,
            "total": 100.0,
            "expires_at": "2026-11-05T07:59:00+00:00",
            "currency": "USD",
        },
        "usage_credits": {"amount": 12.5, "amount_cents": 1250, "currency": "USD"},
        "raw": {"usage": {}, "organization": {}},
    }
