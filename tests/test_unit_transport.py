# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge.transport import (
    DEFAULT_ADDRESS,
    Transport,
    _configure_tcp_keepalive,
    _keepalive_idle,
    parse_address,
)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.mock_logger = MagicMock()
        logger_patcher = patch("arduino.router_bridge.transport.logger", self.mock_logger)
        logger_patcher.start()
        self.addCleanup(logger_patcher.stop)


class TestParseAddress(TransportTest):
    def test_default_address(self):
        self.assertEqual(parse_address(DEFAULT_ADDRESS), ("unix", "/var/run/arduino-router.sock"))

    def test_unix_address(self):
        self.assertEqual(parse_address("unix:///tmp/test.sock"), ("unix", "/tmp/test.sock"))

    def test_tcp_address(self):
        self.assertEqual(parse_address("tcp://localhost:1234"), ("tcp", ("localhost", 1234)))

    def test_invalid_addresses_are_rejected(self):
        for bad_address in (
            "http://localhost:8080",  # unsupported scheme
            "/var/run/arduino-router.sock",  # missing scheme
            "tcp://localhost",  # missing port
            "tcp://localhost:notaport",  # malformed port
            "tcp://:1234",  # missing host
            "unix://",  # missing path
        ):
            with self.assertRaises(ValueError, msg=f"Address '{bad_address}' was not rejected"):
                parse_address(bad_address)

    def test_unix_address_rejected_without_af_unix_support(self):
        """unix:// addresses must fail on platforms lacking AF_UNIX (e.g. Windows)."""
        with patch("arduino.router_bridge.transport.socket") as mock_socket:
            del mock_socket.AF_UNIX
            with self.assertRaises(ValueError):
                parse_address("unix:///tmp/test.sock")


class TestConnect(TransportTest):
    """Connection establishment against a mocked socket module."""

    def setUp(self):
        super().setUp()
        self.mock_sock = MagicMock()
        socket_patcher = patch("arduino.router_bridge.transport.socket")
        self.mock_socket = socket_patcher.start()
        self.addCleanup(socket_patcher.stop)
        self.mock_socket.socket.return_value = self.mock_sock
        self.mock_socket.create_connection.return_value = self.mock_sock

    def test_unix_connect(self):
        Transport.connect("unix", "/tmp/test.sock")
        self.mock_socket.socket.assert_called_once_with(self.mock_socket.AF_UNIX, self.mock_socket.SOCK_STREAM)
        self.mock_sock.connect.assert_called_once_with("/tmp/test.sock")
        self.mock_sock.settimeout.assert_called_once_with(None)  # Blocking recv once established

    def test_tcp_connect(self):
        Transport.connect("tcp", ("localhost", 1234))
        self.mock_socket.create_connection.assert_called_once_with(("localhost", 1234), timeout=5.0)
        self.mock_sock.settimeout.assert_called_once_with(None)

    def test_unix_connect_failure_closes_the_socket(self):
        """A failed connect must not leak the socket it created."""
        self.mock_sock.connect.side_effect = OSError("No such file or directory")
        with self.assertRaises(OSError):
            Transport.connect("unix", "/tmp/never-exists.sock")
        self.mock_sock.close.assert_called_once()

    def test_tcp_enables_keepalive(self):
        """A TCP connection must enable SO_KEEPALIVE so half-open peers are eventually detected."""
        Transport.connect("tcp", ("localhost", 1234))
        self.mock_sock.setsockopt.assert_any_call(self.mock_socket.SOL_SOCKET, self.mock_socket.SO_KEEPALIVE, 1)

    def test_unix_skips_keepalive(self):
        """Unix sockets are torn down by the kernel on peer exit, so keepalive must not be set."""
        Transport.connect("unix", "/tmp/test.sock")
        self.mock_sock.setsockopt.assert_not_called()


class TestKeepalive(TransportTest):
    def setUp(self):
        super().setUp()
        socket_patcher = patch("arduino.router_bridge.transport.socket")
        self.mock_socket = socket_patcher.start()
        self.addCleanup(socket_patcher.stop)

    def test_linux_tunes_timers_via_setsockopt(self):
        """On Linux (TCP_KEEPIDLE present) the idle/interval/count timers are set via setsockopt."""
        sock = MagicMock()
        _configure_tcp_keepalive(sock)
        sock.setsockopt.assert_any_call(self.mock_socket.IPPROTO_TCP, self.mock_socket.TCP_KEEPIDLE, _keepalive_idle)

    def test_missing_timer_knobs_are_skipped(self):
        """On macOS/Windows (no Linux timer knobs) tuning is skipped but SO_KEEPALIVE is still set."""
        for name in ("TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT"):
            delattr(self.mock_socket, name)
        sock = MagicMock()
        _configure_tcp_keepalive(sock)
        sock.setsockopt.assert_called_once_with(self.mock_socket.SOL_SOCKET, self.mock_socket.SO_KEEPALIVE, 1)

    def test_keepalive_failure_is_not_fatal(self):
        """A socket refusing SO_KEEPALIVE must be tolerated: keepalive is an optimization."""
        sock = MagicMock()
        sock.setsockopt.side_effect = OSError("not supported")
        _configure_tcp_keepalive(sock)  # Must not raise
        sock.setsockopt.assert_called_once()  # Timer tuning skipped after the failure


class SocketPairTest(TransportTest):
    """Tests against a real connected socket pair."""

    def make_pair(self):
        local, peer = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(peer.close)
        return Transport(local), peer


class TestSendAll(SocketPairTest):
    def test_sends_all_bytes(self):
        transport, peer = self.make_pair()
        payload = b"x" * 100_000  # Larger than a single send buffer slice

        sender = threading.Thread(target=transport.send_all, args=(payload,))
        sender.start()
        received = b""
        while len(received) < len(payload):
            received += peer.recv(65536)
        sender.join(timeout=2)
        self.assertEqual(received, payload)

    def test_stalled_send_raises_timeout(self):
        """A send that never becomes writable must fail instead of blocking forever."""
        transport = Transport(MagicMock())
        # select reporting no writability stands in for a peer that stopped reading
        with patch("arduino.router_bridge.transport.select.select", return_value=([], [], [])):
            with self.assertRaises(TimeoutError):
                transport.send_all(b"payload")
        transport._sock.send.assert_not_called()  # Never blocked on the wire

    def test_send_failure_raises_oserror(self):
        transport, peer = self.make_pair()
        peer.close()
        transport.close()
        with self.assertRaises(OSError):
            transport.send_all(b"payload")


class TestClose(SocketPairTest):
    def test_close_is_idempotent(self):
        transport, _ = self.make_pair()
        transport.close()
        transport.close()  # Must not raise

    def test_close_wakes_a_blocked_recv(self):
        transport, _ = self.make_pair()
        results = []

        def blocked_recv():
            try:
                results.append(transport.recv(4096))
            except OSError:
                results.append(b"")

        reader = threading.Thread(target=blocked_recv)
        reader.start()
        transport.close()
        reader.join(timeout=2)
        self.assertEqual(results, [b""], "close() did not wake the blocked recv")
