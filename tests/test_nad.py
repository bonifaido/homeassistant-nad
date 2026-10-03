import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.media_player.const import MediaPlayerState
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.nad import NADReceiverCoordinator
from custom_components.nad.config_flow import (
    CommandNotSupportedError,
    NADReceiverConfigFlow,
    _test_telnet_connection,
)
from custom_components.nad.media_player import NAD
from custom_components.nad.nad_client import NADConnectionError, NADSocketClient
from custom_components.nad.number import NADReceiverNumber


class ScriptedSocket:
    """Socket double that returns one scripted reply per request."""

    def __init__(self, replies: list[bytes]) -> None:
        self._replies = iter(replies)
        self._incoming = bytearray()
        self._timeout = 10
        self.sent: list[bytes] = []

    def gettimeout(self):
        return self._timeout

    def settimeout(self, timeout):
        self._timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self._incoming.extend(next(self._replies))

    def recv(self, size: int) -> bytes:
        if not self._incoming:
            raise socket.timeout
        data = bytes(self._incoming[:size])
        del self._incoming[:size]
        return data


def socket_client(*replies: bytes) -> tuple[NADSocketClient, ScriptedSocket]:
    client = NADSocketClient("127.0.0.1", 1, timeout=1)
    fake_socket = ScriptedSocket(list(replies))
    client._sock = fake_socket
    return client, fake_socket


def test_command_parses_decimal_reply_and_uses_lf_framing():
    client, fake_socket = socket_client(b"Main.Volume=-48.0\r\n")

    assert client.command("Main.Volume", "?") == "-48.0"
    assert fake_socket.sent == [b"\nMain.Volume?\n"]


def test_unsupported_command_returns_none_without_waiting_for_timeout():
    client, _ = socket_client(b"Wrong Command\r\n")

    assert client.command("DSP.Version", "?") is None


def test_main_snapshot_parses_c328_state():
    marker = b"************Main information end ************"
    client, fake_socket = socket_client(
        b"Main.Model=C328\r\nMain.Power=On\r\nMain.Volume=-48.0\r\n" + marker
    )

    assert client.main_snapshot() == {
        "Main.Model": "C328",
        "Main.Power": "On",
        "Main.Volume": "-48.0",
    }
    assert fake_socket.sent == [b"\nMain?\n"]


class SnapshotClient(NADSocketClient):
    def __init__(self, *results) -> None:
        super().__init__("127.0.0.1", 1)
        self._results = list(results)

    def main_snapshot(self) -> dict[str, str]:
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class CoordinatorProbe:
    _async_update_data = NADReceiverCoordinator._async_update_data
    _capture_unsolicited = NADReceiverCoordinator._capture_unsolicited
    _main_snapshot_with_reconnect = NADReceiverCoordinator._main_snapshot_with_reconnect
    _reconnect = NADReceiverCoordinator._reconnect

    def __init__(self, hass, first_client, replacement_client) -> None:
        self.hass = hass
        self.receiver = first_client
        self.replacement_client = replacement_client
        self._listener_commands = []
        self._pending_unsolicited = {}
        self.power_state = None
        self.close_count = 0

    def _close_receiver(self) -> None:
        self.close_count += 1

    def _create_receiver(self):
        return self.replacement_client


@pytest.mark.asyncio
async def test_snapshot_reconnects_once_after_kickolduser(hass):
    first = SnapshotClient(NADConnectionError("kicked"))
    replacement = SnapshotClient({"Main.Power": "On", "Main.Model": "C328"})
    coordinator = CoordinatorProbe(hass, first, replacement)

    data = await coordinator._async_update_data()

    assert data["Main.Model"] == "C328"
    assert data["Main.Power"] == "On"
    assert coordinator.close_count == 1
    assert coordinator.receiver is replacement


@pytest.mark.asyncio
async def test_snapshot_reports_update_failure_after_reconnect_fails(hass):
    first = SnapshotClient(NADConnectionError("kicked"))
    replacement = SnapshotClient(NADConnectionError("still unavailable"))
    coordinator = CoordinatorProbe(hass, first, replacement)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.close_count == 1
    assert coordinator.power_state is None


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("3", 0), ("4", 1), ("1", 2), ("2", 3)],
)
def test_c328_brightness_snapshot_maps_to_logical_level(reported, expected):
    probe = SimpleNamespace(
        entity_description=SimpleNamespace(key="Main.Brightness"),
        coordinator=SimpleNamespace(model="C328", data={"Main.Brightness": reported}),
        _attr_native_value=None,
        _attr_available=False,
        async_write_ha_state=Mock(),
    )
    probe._is_c328_brightness = NADReceiverNumber._is_c328_brightness.fget(probe)

    NADReceiverNumber._handle_coordinator_update(probe)

    assert probe._attr_native_value == expected
    assert probe._attr_available


@pytest.mark.asyncio
async def test_c328_brightness_write_uses_documented_value_directly():
    coordinator = SimpleNamespace(
        model="C328",
        power_state=MediaPlayerState.ON,
        exec_command=Mock(return_value="3"),
    )
    probe = SimpleNamespace(
        entity_description=SimpleNamespace(key="Main.Brightness"),
        coordinator=coordinator,
        _attr_native_value=2,
        _attr_available=True,
        step=1,
        async_write_ha_state=Mock(),
    )
    probe._is_c328_brightness = NADReceiverNumber._is_c328_brightness.fget(probe)

    await NADReceiverNumber.async_set_native_value(probe, 3)

    coordinator.exec_command.assert_called_once_with("Main.Brightness", "=", 3)
    assert probe._attr_native_value == 3


def test_volume_db_parser_accepts_decimal_c328_value():
    parsed = NAD._parse_volume_db("-48.0")

    assert parsed == -48.0
    assert NAD.calc_volume(
        SimpleNamespace(_min_volume=-80, _max_volume=12), parsed
    ) == pytest.approx(32 / 92)
    assert NAD._parse_volume_db("not-a-volume") is None
    assert NAD._parse_volume_db(None) is None


def test_telnet_probe_closes_client_when_query_is_unsupported(monkeypatch):
    client = Mock()
    client.command.return_value = None
    monkeypatch.setattr(
        "custom_components.nad.config_flow.NADSocketClient", Mock(return_value=client)
    )

    with pytest.raises(CommandNotSupportedError):
        _test_telnet_connection("receiver.local", 4000)

    client.connect.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_telnet_config_flow_maps_socket_error_to_cannot_connect(
    hass, monkeypatch
):
    flow = NADReceiverConfigFlow()
    flow.hass = hass
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", Mock())
    probe = Mock(side_effect=OSError("unreachable"))
    monkeypatch.setattr(
        "custom_components.nad.config_flow._test_telnet_connection", probe
    )
    errors = {}

    await flow.validate_input_setup_telnet(
        {"host": "receiver.local", "port": 4000}, errors
    )

    assert errors["base"] == "cannot_connect"
    probe.assert_called_once_with("receiver.local", 4000)
