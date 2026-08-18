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

    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._lock = threading.Lock()

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

    def close(self) -> None:
        """Close the connection, if open."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def command(self, command: str, operator: str, value=None) -> Optional[str]:
        """Send a command and return the value from the reply, or None."""
        with self._lock:
            if self._sock is None:
                raise NADConnectionError("Not connected")

            cmd = f"{command}{operator}"
            if value is not None and value != "":
                cmd = f"{cmd}{value}"

            try:
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

    def _read_reply(self, command: str) -> str:
        """Read until the reply matches the command that was sent."""
        prefix = f"{command.lower()}="

        while True:
            while b"\r" not in self._buffer:
                chunk = self._sock.recv(256)
                if not chunk:
                    raise NADConnectionError("Connection closed by remote host")
                self._buffer += chunk

            line, _, self._buffer = self._buffer.partition(b"\r")
            reply = line.strip().decode(errors="replace")
            if reply.lower().startswith(prefix):
                return reply

            _LOGGER.debug(
                "Discarding stale NAD reply while waiting for %s: %s",
                command,
                reply,
            )
