"""Diagnostics for Claude Usage (Settings → Devices & services → Claude Usage → ⋮ → Download diagnostics)."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CF_CLEARANCE, CONF_ORG_ID, CONF_SESSION_KEY, DOMAIN
from .coordinator import ClaudeUsageCoordinator, _optional_failures


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (Settings → Integrations → ⋮ → Download diagnostics)."""
    coordinator: ClaudeUsageCoordinator = hass.data[DOMAIN][entry.entry_id]
    m = coordinator.metrics

    return {
        "config": {
            # Credentials are masked — only show length and prefix for debugging
            CONF_SESSION_KEY: _mask(entry.data.get(CONF_SESSION_KEY, "")),
            CONF_CF_CLEARANCE: _mask(entry.data.get(CONF_CF_CLEARANCE, "")),
            CONF_ORG_ID: entry.data.get(CONF_ORG_ID, ""),
        },
        "metrics": {
            "total_requests": m.total_requests,
            "failed_requests": m.failed_requests,
            "success_rate_pct": _success_rate(m.total_requests, m.failed_requests),
            "cookie_renewals": m.cookie_renewals,
            "last_success_at": m.last_success_at,
            "last_response_ms": m.last_response_ms,
        },
        "optional_endpoint_failures": dict(_optional_failures),
        "last_data": coordinator.data,
    }


def _mask(value: str) -> str:
    """Show only the first 10 chars followed by ***."""
    if not value:
        return ""
    return value[:10] + "***"


def _success_rate(total: int, failed: int) -> float | None:
    if total == 0:
        return None
    return round((total - failed) / total * 100, 1)
