"""The NAD Receiver component."""

import logging
from datetime import timedelta
from typing import Any, Callable, Optional

import homeassistant.helpers.config_validation as cv
import serial
import voluptuous as vol
from homeassistant.components.media_player.const import MediaPlayerState
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_TYPE,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from nad_receiver import NADReceiver, NADReceiverTCP, NADReceiverTelnet

from .nad_client import NADConnectionError, NADSocketClient

from .const import (
    CONF_SERIAL_PORT,
    CONF_TYPE_SERIAL,
    CONF_TYPE_TCP,
    CONF_TYPE_TELNET,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
]


class CommandNotSupportedError(Exception):
    """Error to indicate a command is not supported."""


class NADReceiverCoordinator(DataUpdateCoordinator):
    """NAD Receiver Data Update Coordinator."""

    receiver: NADReceiver = None

    unique_id = None
    model: str = None
    version: str = None
    device_info: DeviceInfo = None

    power_state = None

    _listener_commands = []

    def __init__(self, hass, entry: ConfigEntry):
        """Initialize NAD Receiver Data Update Coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name=__name__,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=5),
        )

        self.config = entry.data
        self.options = entry.options
        self.unique_id = entry.entry_id

        self.receiver = self._create_receiver()

    def _create_receiver(self):
        """Create a fresh receiver/transport instance from the stored config."""
        config_type = self.config[CONF_TYPE]
        if config_type == CONF_TYPE_SERIAL:
            serial_port = self.config[CONF_SERIAL_PORT]
            return NADReceiver(serial_port)
        elif config_type == CONF_TYPE_TELNET:
            host = self.config[CONF_HOST]
            port = self.config[CONF_PORT]
            # Direct, dedicated raw TCP client instead of the generic library's
            # telnet transport: no unneeded IAC negotiation, a longer default
            # timeout, and persistent read buffering across commands.
            client = NADSocketClient(host, port)
            client.connect()
            return client
        elif config_type == CONF_TYPE_TCP:
            host = self.config[CONF_HOST]
            return NADReceiverTCP(host)

    def _close_receiver(self):
        """Best-effort close of the current transport connection."""
        if isinstance(self.receiver, NADSocketClient):
            self.receiver.close()
            return

        transport = getattr(self.receiver, "transport", None)
        try:
            if hasattr(transport, "close_connection"):
                transport.close_connection()
            elif hasattr(transport, "ser") and transport.ser.is_open:
                transport.ser.close()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("Error closing NAD receiver connection: %s", ex)

    def _reconnect(self):
        """Tear down and recreate the receiver connection."""
        _LOGGER.debug("Reconnecting to NAD receiver")
        self._close_receiver()
        self.receiver = self._create_receiver()

    async def connect(self) -> bool:
        if not self.model:
            # Open the connection by requesting the model
            try:
                self.model = self.exec_command("Main.Model", "?")
                self.version = self.exec_command("Main.Version", "?")
            except CommandNotSupportedError:
                return False

            identifiers = {(DOMAIN, self.unique_id)}
            if self.config[CONF_TYPE] == CONF_TYPE_SERIAL:
                identifiers.add((DOMAIN, self.config[CONF_SERIAL_PORT]))

            self.device_info = DeviceInfo(
                identifiers=identifiers,
                name=f"NAD {self.model}",
                model=self.model,
                manufacturer="NAD",
                sw_version=self.version,
            )

            if self.model.replace(" ", "").upper() == "C328":
                self.sources = {
                    source: source
                    for source in (
                        "TV",
                        "PHONO",
                        "COAX1",
                        "COAX2",
                        "OPT1",
                        "OPT2",
                        "STREAM",
                        "BT",
                    )
                }
            else:
                self.sources = self.get_sources()

            return True

    async def disconnect(self):
        self._close_receiver()

    @callback
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        remove_listener = super().async_add_listener(update_callback, context)

        _LOGGER.debug("Adding listener for %s", context)
        if context:
            self.add_listener_command(context)

        return remove_listener

    def add_listener_command(self, command):
        _LOGGER.debug("Adding command %s", command)
        if command not in self._listener_commands:
            self._listener_commands.append(command)

    def get_sources(self) -> {}:
        sources = {}

        for i in range(1, 13):
            try:
                response = self.exec_command(f"Source{i}.Enabled", "?")
                if response is not None and response.lower() == "yes":
                    response = self.exec_command(f"Source{i}.Name", "?")
                    sources[i] = response
            except CommandNotSupportedError:
                break

        return sources

    def supports_command(self, command: str):
        try:
            response = self.exec_command(command, "?")
        except CommandNotSupportedError:
            _LOGGER.debug("%s not supported", command)
            return False

        _LOGGER.debug("%s supported", command)
        if not self.data:
            self.data = {}
        self.data[command] = response

        return True

    def exec_command(self, command: str, operator: str, value: Optional = None):
        if self.config[CONF_TYPE] == CONF_TYPE_SERIAL:
            self.receiver.transport.ser.reset_input_buffer()

        for attempt in (1, 2):
            try:
                if isinstance(self.receiver, NADSocketClient):
                    response = self.receiver.command(command, operator, value)
                    self._capture_unsolicited()
                    return response

                cmd = f"{command}{operator}"
                if value:
                    cmd = f"{cmd}{value}"

                msg = self.receiver.transport.communicate(cmd)
                _LOGGER.debug("sent: '%s' reply: '%s'", command, msg)

                if msg == "":
                    raise CommandNotSupportedError()

                if msg.lower().startswith(command.lower() + "="):
                    return msg.split("=")[1]

                return None
            except UnicodeDecodeError as ex:
                _LOGGER.error(ex)
                return None
            except CommandNotSupportedError:
                raise
            except Exception as ex:  # noqa: BLE001
                # The connection may have been dropped (e.g. ser2net kicked us
                # off, or an idle timeout closed the socket). Reconnect once
                # and retry before giving up.
                if attempt == 2:
                    raise CommandNotSupportedError() from ex
                _LOGGER.debug("Connection error (%s), reconnecting", ex)
                self._reconnect()

        return None

    def _capture_unsolicited(self) -> None:
        """Merge unsolicited NAD state into the next coordinator update."""
        if isinstance(self.receiver, NADSocketClient):
            updates = self.receiver.take_unsolicited()
            if updates:
                _LOGGER.debug("Received unsolicited NAD state: %s", updates)
                if not hasattr(self, "_pending_unsolicited"):
                    self._pending_unsolicited = {}
                self._pending_unsolicited.update(updates)

    async def _async_update_data(self):
        """Fetch data from NAD Receiver."""
        try:
            power_state = await self.hass.async_add_executor_job(
                self.exec_command, "Main.Power", "?"
            )
        except CommandNotSupportedError:
            self.power_state = None
            raise UpdateFailed("Error communicating with NAD Receiver")
        except IOError as ex:
            self.power_state = None
            raise UpdateFailed("Error communicating with NAD Receiver", ex)

        _LOGGER.debug("power_state: %s", power_state)
        if not power_state:
            self.power_state = None
            raise UpdateFailed("Error communicating with NAD Receiver")

        if power_state.lower() == "on":
            self.power_state = MediaPlayerState.ON
        else:
            self.power_state = MediaPlayerState.OFF

        data = {}
        data.update(getattr(self, "_pending_unsolicited", {}))
        self._pending_unsolicited = {}
        data["Main.Power"] = power_state

        for command in self._listener_commands:
            if command not in data:
                try:
                    data[command] = await self.hass.async_add_executor_job(
                        self.exec_command, command, "?"
                    )
                except CommandNotSupportedError:
                    data[command] = None

        return data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NAD Receiver from a config entry."""

    @callback
    def _async_migrate_entity_entry(
        registry_entry: entity_registry.RegistryEntry,
    ) -> dict[str, Any] | None:
        """
        Migrates old unique ID to the new unique ID.
        """
        if entry.data[CONF_TYPE] == CONF_TYPE_SERIAL:
            if registry_entry.unique_id.startswith(f"{entry.data[CONF_SERIAL_PORT]}-"):
                new_unique_id = registry_entry.unique_id.replace(
                    f"{entry.data[CONF_SERIAL_PORT]}-",
                    f"{registry_entry.config_entry_id}-",
                )
                _LOGGER.debug("Migrating entity unique id to %s", new_unique_id)
                return {"new_unique_id": new_unique_id}

        # No migration needed
        return None

    await entity_registry.async_migrate_entries(
        hass, entry.entry_id, _async_migrate_entity_entry
    )

    try:
        receiver_coordinator = NADReceiverCoordinator(hass, entry)

        # Open the connection.
        if not await receiver_coordinator.connect():
            raise ConfigEntryNotReady(f"Unable to connect to NAD receiver")

        _LOGGER.info("NAD receiver is available")
    except serial.SerialException as ex:
        raise ConfigEntryNotReady(f"Unable to connect to NAD receiver") from ex

    entry.runtime_data = receiver_coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    receiver_coordinator: NADReceiverCoordinator = entry.runtime_data
    await receiver_coordinator.disconnect()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Configuration options updated, reloading NAD receiver integration")
    hass.config_entries.async_schedule_reload(entry.entry_id)(entry.entry_id)
