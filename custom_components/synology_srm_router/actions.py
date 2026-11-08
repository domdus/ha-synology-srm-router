"""The requests outside aiosynologysrm's read allowlist: restarts and the firmware check.

The library only sends allowlisted reads, so these few go through its login and transport
(`_ensure_login`, `_post`, `_error_code`), pinned with the library's version in manifest.json.
Each request is fixed here: callers choose neither the API nor the method, only a mesh node id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
from aiosynologysrm import (
    SrmApiError,
    SrmClient,
    SrmConnectionError,
    SrmPermissionError,
    SrmSessionError,
    SrmUnsupportedError,
)

# Version 1 is what SRM's own web interface sends; a higher version answers 103.
RESTART: Final = {"api": "SYNO.Core.System", "method": "reboot", "version": "1"}
# Satellites only; the main router refuses it and is restarted with RESTART.
RESTART_MESH_NODE: Final = {"api": "SYNO.Mesh.System", "method": "reboot", "version": "1"}
# Asks Synology's update server through the router; changes nothing.
FIRMWARE_CHECK: Final = {"api": "SYNO.Core.Upgrade.Server", "method": "check", "version": "1"}
_SESSION_ERRORS: Final = frozenset({106, 107, 119})
_UNSUPPORTED_ERRORS: Final = frozenset({102, 103, 104})
_PERMISSION_ERRORS: Final = frozenset({105})
MAX_NODE_ID: Final = 255


@dataclass(frozen=True, slots=True)
class FirmwareCheck:
    """Whether Synology offers newer firmware for the device, and which version."""

    available: bool | None
    version: str | None

    @classmethod
    def from_api(cls, data: Any) -> FirmwareCheck:
        """Read SRM's answer; anything unexpected is unknown, never "up to date"."""
        data = data if isinstance(data, Mapping) else {}
        available = data.get("available")
        version = data.get("version")
        return cls(
            available=available if isinstance(available, bool) else None,
            version=version[:64] if isinstance(version, str) and version else None,
        )


def _dropped(err: SrmConnectionError) -> bool:
    """Whether the router took the connection and then went away, as it does when restarting.

    A connection that never came up (router off, wrong address) is not a sent restart.
    """
    cause = err.__cause__
    if isinstance(cause, aiohttp.ClientConnectorError | aiohttp.ConnectionTimeoutError):
        return False
    return isinstance(
        cause,
        aiohttp.ServerDisconnectedError
        | aiohttp.ClientPayloadError
        | aiohttp.ClientOSError
        | TimeoutError,
    )


async def _send(client: SrmClient, form: Mapping[str, str], *, restart: bool) -> Any:
    """Send one fixed request with the client's session and return its ``data``."""
    sid = await client._ensure_login()
    for attempt in range(2):
        try:
            envelope = await client._post("entry.cgi", {**form, "_sid": sid})
        except SrmConnectionError as err:
            if restart and _dropped(err):
                return None
            raise
        if envelope["success"]:
            return envelope.get("data")
        code = client._error_code(envelope)
        if code in _SESSION_ERRORS and not attempt:
            sid = await client._ensure_login(stale_sid=sid)
            continue
        if code in _SESSION_ERRORS:
            raise SrmSessionError(code, form["api"])
        if code in _UNSUPPORTED_ERRORS:
            raise SrmUnsupportedError(code, form["api"])
        if code in _PERMISSION_ERRORS:
            raise SrmPermissionError(code, form["api"])
        raise SrmApiError(code, form["api"])
    raise SrmApiError(106, form["api"])  # pragma: no cover - the loop returns or raises


async def restart(client: SrmClient) -> None:
    """Restart the whole device; with it the mesh it controls."""
    await _send(client, RESTART, restart=True)


async def restart_mesh_node(client: SrmClient, node_id: int) -> None:
    """Restart one mesh satellite of a router."""
    if type(node_id) is not int or not 0 <= node_id <= MAX_NODE_ID:
        msg = "Invalid mesh node id"
        raise ValueError(msg)
    await _send(client, {**RESTART_MESH_NODE, "node_id_list": f"[{node_id}]"}, restart=True)


async def check_firmware(client: SrmClient) -> FirmwareCheck:
    """Ask whether newer firmware is available."""
    return FirmwareCheck.from_api(await _send(client, FIRMWARE_CHECK, restart=False))
