"""Restart buttons, the firmware entity and the fixed requests behind them."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp
from aiosynologysrm import (
    SrmApiError,
    SrmClient,
    SrmConnectionError,
    SrmPermissionError,
    SrmSessionError,
    SrmTimeoutError,
    SrmUnsupportedError,
)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import (
    DATA_COMPONENT as BUTTON_COMPONENT,
    DOMAIN as BUTTON_DOMAIN,
    SERVICE_PRESS,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.synology_srm_router import actions
from custom_components.synology_srm_router.actions import FirmwareCheck
from custom_components.synology_srm_router.const import FIRMWARE_INTERVAL, FIRMWARE_RETRY

from .conftest import AP_HOST, MESH, POSTS, ROUTER_HOST, SERIALS, FakeClient, make_entry

ROUTER = SERIALS["router"]
SATELLITE_MESH: dict[str, Any] = {
    "total": 2,
    "nodes": [
        {"node_id": 0, "name": "name-1", "parent_node_id": -1},
        {"node_id": 3, "name": "name-2", "parent_node_id": 0},
    ],
}


async def _setup(hass: HomeAssistant, kind: str = "router") -> Any:
    entry = make_entry(kind)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity(hass: HomeAssistant, domain: str, key: str, serial: str = ROUTER) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, "synology_srm_router", f"{serial}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


def _slow(hass: HomeAssistant) -> Any:
    return hass.config_entries.async_entries("synology_srm_router")[0].runtime_data.slow


def _client(host: str = ROUTER_HOST) -> FakeClient:
    return next(c for c in reversed(FakeClient.instances) if c.host == host)


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _dropped(cause: BaseException) -> SrmConnectionError:
    err = SrmConnectionError("Connection failed")
    err.__cause__ = cause
    return err


def _key() -> aiohttp.client_reqrep.ConnectionKey:
    return aiohttp.client_reqrep.ConnectionKey(ROUTER_HOST, 8001, True, True, None, None, None)


# Restart buttons


async def test_router_and_access_point_have_a_restart_button(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _setup(hass, "ap")
    router = _entity(hass, "button", "restart")
    entry = er.async_get(hass).async_get(router)
    assert entry is not None
    assert entry.entity_category == "config"
    assert hass.states.get(router).attributes["device_class"] == "restart"  # type: ignore[union-attr]
    assert _entity(hass, "button", "restart", SERIALS["ap"])


async def test_restart_sends_the_fixed_request(hass: HomeAssistant) -> None:
    await _setup(hass)
    client = _client()
    client.posts.clear()
    await _press(hass, _entity(hass, "button", "restart"))
    ((cgi, form),) = client.posts
    assert cgi == "entry.cgi"
    assert form == {"api": "SYNO.Core.System", "method": "reboot", "version": "1", "_sid": "sid-2"}


@pytest.mark.parametrize(
    "cause",
    [
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("cut"),
        aiohttp.ClientOSError(104, "reset"),
        TimeoutError(),
    ],
)
async def test_a_dropped_connection_counts_as_a_sent_restart(
    hass: HomeAssistant, cause: BaseException
) -> None:
    await _setup(hass)
    POSTS[ROUTER_HOST] = [_dropped(cause)]
    await _press(hass, _entity(hass, "button", "restart"))


@pytest.mark.parametrize(
    "error",
    [
        _dropped(aiohttp.ClientConnectorError(_key(), OSError(113, "no route"))),
        _dropped(aiohttp.ConnectionTimeoutError()),
        SrmConnectionError("no cause"),
    ],
)
async def test_an_unreachable_router_is_not_restarted(
    hass: HomeAssistant, error: BaseException
) -> None:
    await _setup(hass)
    POSTS[ROUTER_HOST] = [error]
    with pytest.raises(HomeAssistantError) as caught:
        await _press(hass, _entity(hass, "button", "restart"))
    assert caught.value.translation_key == "restart_failed"


async def test_a_failed_login_is_not_a_restart(hass: HomeAssistant) -> None:
    await _setup(hass)
    client = _client()
    client.errors["login"] = SrmConnectionError("down")
    client.posts.clear()
    with pytest.raises(HomeAssistantError):
        await _press(hass, _entity(hass, "button", "restart"))
    assert not client.posts


async def test_restart_stays_available_while_reads_fail(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    client = _client()
    client.errors["get_system_info"] = SrmConnectionError("down")
    freezer.tick(timedelta(minutes=6))
    await _slow(hass).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "button", "restart")).state != STATE_UNAVAILABLE  # type: ignore[union-attr]


async def test_mesh_satellites_get_their_own_button(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    MESH[ROUTER_HOST] = SATELLITE_MESH
    await _setup(hass)
    assert (
        er.async_get(hass).async_get_entity_id(
            "button", "synology_srm_router", f"{ROUTER}_restart_node_0"
        )
        is None
    )
    node = _entity(hass, "button", "restart_node_3")
    assert hass.states.get(node).name.endswith("Restart name-2")  # type: ignore[union-attr]
    client = _client()
    client.posts.clear()
    await _press(hass, node)
    ((_, form),) = client.posts
    assert (form["api"], form["method"], form["node_id_list"]) == (
        "SYNO.Mesh.System",
        "reboot",
        "[3]",
    )
    # The node leaves the mesh: the button turns unavailable and refuses a press.
    MESH[ROUTER_HOST] = {"total": 1, "nodes": SATELLITE_MESH["nodes"][:1]}
    freezer.tick(timedelta(minutes=6))
    await _slow(hass).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(node).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    entity = hass.data[BUTTON_COMPONENT].get_entity(node)
    assert entity is not None
    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_press()
    assert caught.value.translation_key == "node_gone"


async def test_a_failed_mesh_restart_is_reported(hass: HomeAssistant) -> None:
    MESH[ROUTER_HOST] = SATELLITE_MESH
    await _setup(hass)
    POSTS[ROUTER_HOST] = [{"success": False, "error": {"code": 105}}]
    with pytest.raises(HomeAssistantError) as caught:
        await _press(hass, _entity(hass, "button", "restart_node_3"))
    assert caught.value.translation_placeholders == {"error": "SrmPermissionError"}


async def test_access_points_do_not_read_the_mesh(hass: HomeAssistant) -> None:
    MESH[AP_HOST] = SATELLITE_MESH
    await _setup(hass, "ap")
    assert (
        er.async_get(hass).async_get_entity_id(
            "button", "synology_srm_router", f"{SERIALS['ap']}_restart_node_3"
        )
        is None
    )


# Firmware


async def test_firmware_is_up_to_date(hass: HomeAssistant) -> None:
    await _setup(hass)
    state = hass.states.get(_entity(hass, "update", "firmware"))
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes["installed_version"] == "SRM 1.3.2-9366 Update 3"
    assert state.attributes["latest_version"] == "SRM 1.3.2-9366 Update 3"
    assert state.attributes["title"] == "Synology Router Manager"


async def test_newer_firmware_is_offered_and_checked_twice_a_day(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    POSTS[ROUTER_HOST] = [{"success": True, "data": {"available": True, "version": "1.3.2-9400"}}]
    await _setup(hass)
    firmware = _entity(hass, "update", "firmware")
    assert hass.states.get(firmware).state == STATE_ON  # type: ignore[union-attr]
    assert hass.states.get(firmware).attributes["latest_version"] == "1.3.2-9400"  # type: ignore[union-attr]
    client = _client()
    checks = sum(form["api"] == "SYNO.Core.Upgrade.Server" for _, form in client.posts)
    freezer.tick(timedelta(minutes=6))
    await _slow(hass).async_refresh()
    await hass.async_block_till_done()
    assert sum(form["api"] == "SYNO.Core.Upgrade.Server" for _, form in client.posts) == checks
    # The answer is kept between checks.
    assert hass.states.get(firmware).state == STATE_ON  # type: ignore[union-attr]
    freezer.tick(FIRMWARE_INTERVAL)
    await _slow(hass).async_refresh()
    await hass.async_block_till_done()
    assert sum(form["api"] == "SYNO.Core.Upgrade.Server" for _, form in client.posts) == checks + 1
    assert hass.states.get(firmware).state == STATE_OFF  # type: ignore[union-attr]


async def test_a_failed_check_keeps_the_last_answer_and_retries_sooner(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    firmware = _entity(hass, "update", "firmware")
    coordinator = hass.config_entries.async_entries("synology_srm_router")[0].runtime_data.slow
    POSTS[ROUTER_HOST] = [SrmConnectionError("down")]
    freezer.tick(FIRMWARE_INTERVAL)
    await _slow(hass).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(firmware).state == STATE_OFF  # type: ignore[union-attr]
    assert coordinator.firmware_due == dt_util.utcnow() + FIRMWARE_RETRY


async def test_a_device_without_the_check_stops_asking(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    POSTS[ROUTER_HOST] = [{"success": False, "error": {"code": 102}}]
    await _setup(hass)
    firmware = _entity(hass, "update", "firmware")
    assert hass.states.get(firmware).state == "unknown"  # type: ignore[union-attr]
    client = _client()
    checks = sum(form["api"] == "SYNO.Core.Upgrade.Server" for _, form in client.posts)
    freezer.tick(FIRMWARE_INTERVAL * 3)
    await _slow(hass).async_refresh()
    await hass.async_block_till_done()
    assert sum(form["api"] == "SYNO.Core.Upgrade.Server" for _, form in client.posts) == checks


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"available": True, "version": "x"}, FirmwareCheck(available=True, version="x")),
        ({"available": "yes", "version": ""}, FirmwareCheck(available=None, version=None)),
        (None, FirmwareCheck(available=None, version=None)),
    ],
)
def test_firmware_answers(data: Any, expected: FirmwareCheck) -> None:
    assert FirmwareCheck.from_api(data) == expected


async def test_offered_but_unnamed_firmware_is_unknown(hass: HomeAssistant) -> None:
    POSTS[ROUTER_HOST] = [{"success": True, "data": {"available": True}}]
    await _setup(hass)
    state = hass.states.get(_entity(hass, "update", "firmware"))
    assert state is not None
    assert state.attributes["latest_version"] is None


# The fixed requests


@pytest.mark.parametrize("node_id", [-1, 256, True, "3"])
async def test_mesh_node_ids_are_checked_before_sending(node_id: Any) -> None:
    client = FakeClient(ROUTER_HOST, "u", "p")
    with pytest.raises(ValueError, match="mesh node id"):
        await actions.restart_mesh_node(client, node_id)  # type: ignore[arg-type]
    assert not client.posts


@pytest.mark.parametrize(
    ("code", "error"),
    [(102, SrmUnsupportedError), (105, SrmPermissionError), (117, SrmApiError)],
)
async def test_errors_of_the_requests(code: int, error: type[Exception]) -> None:
    client = FakeClient(ROUTER_HOST, "u", "p")
    POSTS[ROUTER_HOST] = [{"success": False, "error": {"code": code}}]
    with pytest.raises(error):
        await actions.restart(client)  # type: ignore[arg-type]


async def test_an_expired_session_logs_in_once_more() -> None:
    client = FakeClient(ROUTER_HOST, "u", "p")
    POSTS[ROUTER_HOST] = [{"success": False, "error": {"code": 119}}, {"success": True}]
    await actions.restart(client)  # type: ignore[arg-type]
    assert [form["_sid"] for _, form in client.posts] == ["sid-1", "sid-2"]
    POSTS[ROUTER_HOST] = [{"success": False, "error": {"code": 106}}] * 2
    with pytest.raises(SrmSessionError):
        await actions.restart(client)  # type: ignore[arg-type]


async def test_a_dropped_firmware_check_is_an_error() -> None:
    client = FakeClient(ROUTER_HOST, "u", "p")
    POSTS[ROUTER_HOST] = [_dropped(aiohttp.ServerDisconnectedError())]
    with pytest.raises(SrmConnectionError):
        await actions.check_firmware(client)  # type: ignore[arg-type]


async def test_the_real_client_carries_the_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    # The private parts of the library that actions.py relies on, against the pinned version.
    base = f"http://{ROUTER_HOST}:8000/webapi"
    aioclient_mock.post(f"{base}/query.cgi", json={"success": True, "data": {}})
    aioclient_mock.post(f"{base}/auth.cgi", json={"success": True, "data": {"sid": "abc"}})
    aioclient_mock.post(f"{base}/entry.cgi", json={"success": True})
    client = SrmClient(
        ROUTER_HOST,
        "user",
        "secret",
        session=async_get_clientsession(hass),
        port=8000,
        use_https=False,
    )
    await actions.restart(client)
    method, url, data, _ = aioclient_mock.mock_calls[-1]
    assert (method, str(url)) == ("POST", f"{base}/entry.cgi")
    assert data == {"api": "SYNO.Core.System", "method": "reboot", "version": "1", "_sid": "abc"}


async def test_the_real_client_reports_a_timeout_as_dropped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    base = f"http://{ROUTER_HOST}:8000/webapi"
    aioclient_mock.post(f"{base}/query.cgi", json={"success": True, "data": {}})
    aioclient_mock.post(f"{base}/auth.cgi", json={"success": True, "data": {"sid": "abc"}})
    aioclient_mock.post(f"{base}/entry.cgi", exc=TimeoutError())
    client = SrmClient(
        ROUTER_HOST,
        "user",
        "secret",
        session=async_get_clientsession(hass),
        port=8000,
        use_https=False,
    )
    await actions.restart(client)
    with pytest.raises(SrmTimeoutError):
        await actions.check_firmware(client)
