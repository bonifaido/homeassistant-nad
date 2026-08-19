"""Dedicated, minimal TCP client for NAD ASCII protocol receivers (e.g. C328)
reachable over a serial-to-network bridge such as ser2net or esp-link.

This exists because the generic `nad_receiver` library's telnet transport
performs real telnet IAC negotiation, hardcodes a 1 second timeout, and is
used synchronously inside async code paths. Over a raw ser2net TCP bridge
none of the telnet negotiation is needed, and the tight timeout combined
with no persistent read buffering caused intermittent disconnects and
flapping entity availability.
"""

import logging
import socket
import threading
from typing import Optional

_LOGGER = logging.getLogger(__name__)


class NADConnectionError(Exception):
    """Raised when the connection to the NAD receiver is lost or fails."""


class NADSocketClient:
    """Plain TCP client for the NAD ASCII command protocol.

    Commands are framed as ``\\n{command}{operator}{value}\\r`` and replies
    are terminated with ``\\r``, matching the behaviour observed directly
    against a NAD C328 over a ser2net raw TCP bridge.
    """

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._max_buffer_size = 8192
        self._unsolicited: dict[str, str] = {}

    def connect(self) -> None:
        """Open (or reopen) the TCP connection."""
        self.close()

        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        sock.settimeout(self._timeout)

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

        self._sock = sock
        self._buffer = b""
        self._drain_pending_input()

    def close(self) -> None:
        """Close the connection, if open."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""
        self._unsolicited = {}

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def take_unsolicited(self) -> dict[str, str]:
        """Return and clear valid state messages received out of band."""
        with self._lock:
            updates = self._unsolicited
            self._unsolicited = {}
            return updates

    def _save_unsolicited(self, reply: str) -> None:
        """Save a valid command/value line emitted without a request."""
        if "=" not in reply:
            return

        command, value = reply.split("=", 1)
        if command and value:
            self._unsolicited[command] = value

    def _drain_pending_input(self) -> None:
        """Discard output already queued by earlier commands or button presses."""
        if self._sock is None:
            return

        original_timeout = self._sock.gettimeout()
        self._sock.settimeout(0.05)
        try:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    self._buffer += chunk
                    while b"\r" in self._buffer:
                        line, _, self._buffer = self._buffer.partition(b"\r")
                        reply = line.strip().decode(errors="replace")
                        _LOGGER.debug("Draining unsolicited NAD text: %s", reply)
                        self._save_unsolicited(reply)
                except socket.timeout:
                    break
        finally:
            self._sock.settimeout(original_timeout)

    def command(self, command: str, operator: str, value=None) -> Optional[str]:
        """Send a command and return the value from the reply, or None."""
        with self._lock:
            if self._sock is None:
                raise NADConnectionError("Not connected")

            cmd = f"{command}{operator}"
            if value is not None and value != "":
                cmd = f"{cmd}{value}"

            try:
                self._drain_pending_input()
                self._sock.sendall(f"\n{cmd}\r".encode())
                reply = self._read_reply(command)
            except (OSError, socket.timeout) as ex:
                raise NADConnectionError(str(ex)) from ex

            if not reply:
                return None

            _LOGGER.debug("sent: '%s' reply: '%s'", cmd, reply)

            prefix = f"{command.lower()}="
            if reply.lower().startswith(prefix):
                return reply.split("=", 1)[1]

            return None

    def main_snapshot(self) -> dict[str, str]:
        """Return the complete Main? status snapshot from a C328."""
        with self._lock:
            if self._sock is None:
                raise NADConnectionError("Not connected")

            try:
                self._drain_pending_input()
                self._sock.sendall(b"\nMain?\r")
                payload = self._read_until_marker(
                    b"************Main information end ************"
                )
            except (OSError, socket.timeout) as ex:
                raise NADConnectionError(str(ex)) from ex

        snapshot = {}
        for line in payload.decode(errors="replace").splitlines():
            if "=" not in line:
                continue
            command, value = line.strip().split("=", 1)
            if command.startswith("Main."):
                snapshot[command] = value

        _LOGGER.debug("received Main? snapshot: %s", snapshot)
        return snapshot

    def _read_until_marker(self, marker: bytes) -> bytes:
        """Read a response containing a complete marker-delimited payload."""
        while marker not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise NADConnectionError("Connection closed by remote host")
            self._buffer += chunk
            if len(self._buffer) > self._max_buffer_size:
                raise NADConnectionError("Oversized NAD snapshot")

        payload, _, self._buffer = self._buffer.partition(marker)
        return payload

    def _read_reply(self, command: str) -> str:
        """Read until the reply matches the command that was sent."""
        prefix = f"{command.lower()}="

        while True:
            while b"\r" not in self._buffer:
                chunk = self._sock.recv(256)
                if not chunk:
                    raise NADConnectionError("Connection closed by remote host")
                self._buffer += chunk
                if len(self._buffer) > self._max_buffer_size:
                    raise NADConnectionError("Unterminated NAD response")

            line, _, self._buffer = self._buffer.partition(b"\r")
            reply = line.strip().decode(errors="replace")
            if reply.lower().startswith(prefix):
                return reply

            _LOGGER.debug(
                "Discarding unsolicited NAD text while waiting for %s: %s",
                command,
                reply,
            )
            self._save_unsolicited(reply)
