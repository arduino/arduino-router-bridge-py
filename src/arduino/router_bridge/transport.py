# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

"""Socket layer: address parsing and the Transport wrapping one established connection."""

import logging
import select
import socket
import threading
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_ADDRESS = "unix:///var/run/arduino-router.sock"

_connect_timeout = 5.0  # Seconds, bounds a TCP connection attempt
_send_timeout = 15.0  # Seconds, bounds a blocking send so a stalled peer cannot block the send path

# TCP keepalive detects a half-open connection (peer vanished without FIN).
# The timers below are Linux-only and skipped elsewhere, where the OS defaults apply.
_keepalive_idle = 10  # Seconds idle before the first probe
_keepalive_interval = 5  # Seconds between probes
_keepalive_count = 3  # Unanswered probes before the connection is considered dead


def parse_address(address: str) -> tuple[str, str | tuple[str, int]]:
    """Parses "unix://<path>" or "tcp://<host>:<port>" into a ("unix", path) or
    ("tcp", (host, port)) pair, raising ValueError for anything else.
    """
    urlparsed = urlparse(address)
    if urlparsed.scheme == "unix":
        if not hasattr(socket, "AF_UNIX"):
            raise ValueError(
                f"Invalid address '{address}': unix:// sockets are not supported on this platform. "
                "The router runs on the Linux board; use tcp:// only for development."
            )
        if not urlparsed.path:
            raise ValueError(f"Invalid unix address '{address}': expected unix://<path>.")
        return "unix", urlparsed.path
    elif urlparsed.scheme == "tcp":
        try:
            port = urlparsed.port
        except ValueError as e:
            raise ValueError(f"Invalid tcp address '{address}': {e}") from e
        if not urlparsed.hostname or not port:
            raise ValueError(f"Invalid tcp address '{address}': expected tcp://<host>:<port>.")
        return "tcp", (urlparsed.hostname, port)
    else:
        raise ValueError(
            f"Unsupported scheme '{urlparsed.scheme}' in address '{address}': "
            "expected unix://<path> or tcp://<host>:<port>."
        )


class Transport:
    """One established socket connection to the router. The reconnect logic creates a fresh
    Transport per connection attempt, so "which connection is this?" is an identity question.
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._send_lock = threading.Lock()  # Serializes socket writes

    @classmethod
    def connect(cls, socket_type: str, peer_addr) -> "Transport":
        """Opens a blocking connection to peer_addr, raising OSError on failure."""
        if socket_type == "unix":
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(peer_addr)
            except BaseException:
                sock.close()
                raise
        else:
            host, port = peer_addr
            sock = socket.create_connection((host, port), timeout=_connect_timeout)
        sock.settimeout(None)  # Set blocking recv
        if socket_type == "tcp":
            _configure_tcp_keepalive(sock)
        return cls(sock)

    def recv(self, bufsize: int) -> bytes:
        return self._sock.recv(bufsize)

    def send_all(self, data: bytes):
        """Sends all bytes, bounding how long each write may block so a peer that stops reading
        cannot wedge the send path. Raises TimeoutError on a stall, OSError on socket failure.
        """
        view = memoryview(data)
        total = view.nbytes
        sent = 0
        # The send lock only serializes writers; close() never takes it, so it can always wake a blocked send
        with self._send_lock:
            while sent < total:
                try:
                    _, writable, _ = select.select([], [self._sock], [], _send_timeout)
                except ValueError:
                    # select rejects the -1 fd of a socket closed concurrently: normalize to a socket error
                    raise OSError("Socket closed during send") from None
                if not writable:
                    raise TimeoutError(f"Send stalled for {_send_timeout}s")
                # select reported writability, so this send accepts at least one byte without blocking
                sent += self._sock.send(view[sent:])

    def close(self):
        """Shuts the connection down and releases the socket. Idempotent, never raises, and
        safe from any thread: shutdown wakes a recv()/send() blocked elsewhere.
        """
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Already disconnected
        try:
            self._sock.close()  # Release resources
        except Exception:
            pass


def _configure_tcp_keepalive(sock: socket.socket):
    """Enables keepalive so a half-open connection is detected instead of appearing healthy
    forever. The timers are tuned on Linux, development-only platforms keep the OS defaults.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as e:
        logger.debug(f"Could not enable SO_KEEPALIVE: {e}")
        return

    for optname, value in (
        ("TCP_KEEPIDLE", _keepalive_idle),
        ("TCP_KEEPINTVL", _keepalive_interval),
        ("TCP_KEEPCNT", _keepalive_count),
    ):
        opt = getattr(socket, optname, None)
        if opt is None:
            continue  # Not available on this platform, skip it
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError as e:
            logger.debug(f"Could not set {optname}: {e}")
