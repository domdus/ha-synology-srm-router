"""Restart buttons, as Synology DSM offers them for a NAS: the device, and each mesh satellite."""

from __future__ import annotations

from aiosynologysrm import MeshNode, SrmError
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .actions import restart, restart_mesh_node
from .const import DOMAIN, LOGGER
from .coordinator import SlowData, SrmConfigEntry, SrmSlowCoordinator
from .entity import SrmEntity

# One restart at a time.
PARALLEL_UPDATES = 1

RESTART = ButtonEntityDescription(
    key="restart",
    translation_key="restart",
    device_class=ButtonDeviceClass.RESTART,
    entity_category=EntityCategory.CONFIG,
)


def satellites(data: SlowData | None) -> dict[int, MeshNode]:
    """Return the mesh points below the main router by node id; the router itself is not one."""
    nodes = data.mesh.nodes if data is not None and data.mesh is not None else ()
    return {
        node.node_id: node
        for node in nodes
        if node.node_id is not None and node.parent_node_id not in (None, -1)
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the restart button of the device, and one per mesh satellite as they appear."""
    slow = entry.runtime_data.slow
    async_add_entities([SrmRestartButton(entry, slow, RESTART)])
    added: set[int] = set()

    @callback
    def _update() -> None:
        new = [
            SrmMeshNodeRestartButton(entry, slow, node_id, node.name)
            for node_id, node in satellites(slow.data).items()
            if node_id not in added
        ]
        if new:
            added.update(button.node_id for button in new)
            async_add_entities(new)

    _update()
    entry.async_on_unload(slow.async_add_listener(_update))


def _failed(err: SrmError) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="restart_failed",
        translation_placeholders={"error": type(err).__name__},
    )


class SrmRestartButton(SrmEntity[SrmSlowCoordinator], ButtonEntity):
    """Restarts the router or access point."""

    @property
    def available(self) -> bool:
        """Stay usable while reads fail: a hung router is when a restart is wanted most."""
        return True

    async def async_press(self) -> None:
        """Send the restart; the router drops the connection while going down."""
        LOGGER.warning("Restarting %s on request", self.coordinator.config_entry.title)
        try:
            await restart(self.coordinator.client)
        except SrmError as err:
            raise _failed(err) from err


class SrmMeshNodeRestartButton(SrmEntity[SrmSlowCoordinator], ButtonEntity):
    """Restarts one mesh satellite of a router."""

    def __init__(
        self,
        entry: SrmConfigEntry,
        coordinator: SrmSlowCoordinator,
        node_id: int,
        name: str | None,
    ) -> None:
        """Bind the button to a node id; its name comes from SRM."""
        super().__init__(
            entry,
            coordinator,
            ButtonEntityDescription(
                key=f"restart_node_{node_id}",
                translation_key="restart_node",
                translation_placeholders={"node": name or str(node_id)},
                device_class=ButtonDeviceClass.RESTART,
                entity_category=EntityCategory.CONFIG,
            ),
        )
        self.node_id = node_id

    @property
    def available(self) -> bool:
        """Available while SRM lists the node as a satellite."""
        return super().available and self.node_id in satellites(self.coordinator.data)

    async def async_press(self) -> None:
        """Send the restart, after checking again that the node is still a satellite."""
        if self.node_id not in satellites(self.coordinator.data):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="node_gone")
        LOGGER.warning("Restarting mesh node %s on request", self.node_id)
        try:
            await restart_mesh_node(self.coordinator.client, self.node_id)
        except SrmError as err:
            raise _failed(err) from err
