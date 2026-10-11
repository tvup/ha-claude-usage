"""Sensor platform for Claude Usage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ClaudeUsageCoordinator


@dataclass(kw_only=True)
class ClaudeUsageSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with a value extractor.

    value_fn   : reads from coordinator.data
    attrs_fn   : extra state attributes from coordinator.data
    """
    value_fn: Callable[[dict], Any] = field(default=lambda _: None)
    attrs_fn: Callable[[dict], dict[str, Any] | None] | None = field(default=None)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _slot_attrs(slot: str) -> Callable[[dict], dict[str, Any]]:
    return lambda d: {"resets_at": (d.get(slot) or {}).get("resets_at")}


def _cents_to_amount(value: Any) -> float | None:
    return round(value / 100, 2) if isinstance(value, (int, float)) else None


SENSORS: tuple[ClaudeUsageSensorDescription, ...] = (
    ClaudeUsageSensorDescription(
        key="session_utilization",
        name="Session Usage",
        icon="mdi:clock-outline",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: (d.get("session_5h") or {}).get("utilization"),
        attrs_fn=_slot_attrs("session_5h"),
    ),
    ClaudeUsageSensorDescription(
        key="session_reset_minutes",
        name="Session Resets In",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: (d.get("session_5h") or {}).get("resets_in_minutes"),
    ),
    ClaudeUsageSensorDescription(
        key="weekly_utilization",
        name="Weekly Usage",
        icon="mdi:calendar-week",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: (d.get("weekly") or {}).get("utilization"),
        attrs_fn=_slot_attrs("weekly"),
    ),
    ClaudeUsageSensorDescription(
        key="weekly_reset_minutes",
        name="Weekly Resets In",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: (d.get("weekly") or {}).get("resets_in_minutes"),
    ),
    ClaudeUsageSensorDescription(
        key="subscription_type",
        name="Plan",
        icon="mdi:card-account-details-outline",
        value_fn=lambda d: d.get("subscription_type"),
    ),
    ClaudeUsageSensorDescription(
        key="limit_resets_count",
        name="Limit Resets",
        icon="mdi:restore",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: (d.get("limit_resets") or {}).get("count"),
        attrs_fn=lambda d: {"next_expires_at": (d.get("limit_resets") or {}).get("next_expires_at")},
    ),
    ClaudeUsageSensorDescription(
        key="cloud_credits_left",
        name="Cloud Credits Left",
        icon="mdi:cloud-outline",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: (d.get("cloud_credits") or {}).get("remaining"),
        attrs_fn=lambda d: {"currency": (d.get("cloud_credits") or {}).get("currency")},
    ),
    ClaudeUsageSensorDescription(
        key="cloud_credits_total",
        name="Cloud Credits Total",
        icon="mdi:cloud",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: (d.get("cloud_credits") or {}).get("total"),
        attrs_fn=lambda d: {"currency": (d.get("cloud_credits") or {}).get("currency")},
    ),
    ClaudeUsageSensorDescription(
        key="cloud_credits_expires_at",
        name="Cloud Credits Expires",
        icon="mdi:cloud-clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _timestamp((d.get("cloud_credits") or {}).get("expires_at")),
    ),
    ClaudeUsageSensorDescription(
        key="usage_credits",
        name="Usage Credits",
        icon="mdi:wallet-outline",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: (d.get("usage_credits") or {}).get("amount"),
        attrs_fn=lambda d: {
            "currency": (d.get("usage_credits") or {}).get("currency"),
            "amount_cents": (d.get("usage_credits") or {}).get("amount_cents"),
        },
    ),
    ClaudeUsageSensorDescription(
        key="usage_credits_enabled",
        name="Usage Credits Enabled",
        icon="mdi:toggle-switch-outline",
        value_fn=lambda d: "on" if (d.get("extra_usage") or {}).get("enabled") else "off",
    ),
    ClaudeUsageSensorDescription(
        key="monthly_spend_limit",
        name="Monthly Spend Limit",
        icon="mdi:cash-lock",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: _cents_to_amount((d.get("extra_usage") or {}).get("limit")),
        attrs_fn=lambda d: {
            "currency": (d.get("extra_usage") or {}).get("currency"),
            "limit_cents": (d.get("extra_usage") or {}).get("limit"),
        },
    ),
    ClaudeUsageSensorDescription(
        key="monthly_spend_used",
        name="Monthly Spend Used",
        icon="mdi:cash-minus",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: _cents_to_amount((d.get("extra_usage") or {}).get("used")),
        attrs_fn=lambda d: {
            "currency": (d.get("extra_usage") or {}).get("currency"),
            "used_cents": (d.get("extra_usage") or {}).get("used"),
        },
    ),
)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ClaudeUsageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ClaudeUsageSensor(coordinator, description)
            for description in SENSORS
        ]
    )


class ClaudeUsageSensor(CoordinatorEntity[ClaudeUsageCoordinator], SensorEntity):
    """A single Claude Usage sensor entity."""

    entity_description: ClaudeUsageSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ClaudeUsageCoordinator,
        description: ClaudeUsageSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Claude Usage",
            manufacturer="Anthropic",
            model="Claude Max",
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None or self.coordinator.data is None:
            return None
        return attrs_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

