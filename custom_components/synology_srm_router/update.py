"""Firmware status as Synology DSM shows it: installed and offered version, no installing."""

from __future__ import annotations

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SrmConfigEntry, SrmSlowCoordinator
from .entity import SrmEntity

PARALLEL_UPDATES = 0

FIRMWARE = UpdateEntityDescription(
    key="firmware",
    translation_key="firmware",
    device_class=UpdateDeviceClass.FIRMWARE,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the firmware entity of one SRM device."""
    async_add_entities([SrmFirmwareUpdate(entry, entry.runtime_data.slow, FIRMWARE)])


class SrmFirmwareUpdate(SrmEntity[SrmSlowCoordinator], UpdateEntity):
    """The device's SRM version and whether Synology offers a newer one."""

    _attr_title = "Synology Router Manager"

    @property
    def installed_version(self) -> str | None:
        """The firmware SRM reports, e.g. ``SRM 1.3.2-9366 Update 3``."""
        return self.coordinator.data.system.firmware_ver

    @property
    def latest_version(self) -> str | None:
        """The offered version; the installed one when none is offered; unknown before a check."""
        check = self.coordinator.data.firmware
        if check is None or check.available is None:
            return None
        if not check.available:
            return self.installed_version
        return check.version
