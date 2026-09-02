# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import gc
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from test_unit_common import UnitTest

from arduino.router_bridge import Bridge
from arduino.router_bridge.connection import _BridgeConnection


class TestBridgeHandle(UnitTest):
    @contextmanager
    def idle_conn_manager(self):
        """Replaces the connection manager loop with a fake that reports itself connected
        without touching any socket, so a blocking connect() returns."""

        def run(conn):
            conn._is_connected_flag.set()
            conn._stop_event.wait()

        with patch.object(_BridgeConnection, "_conn_manager", run):
            yield

    def test_public_api_delegates_to_connection(self):
        """The Bridge handle must delegate every public operation to its connection."""
        bridge = Bridge()
        bridge._connection = MagicMock()

        bridge.connect(timeout=1)
        bridge._connection.start.assert_called_once()
        bridge._connection.wait_connected.assert_called_once_with(1)

        bridge.notify("a_method", 1, 2)
        bridge._connection.notify.assert_called_once_with("a_method", 1, 2)

        self.assertEqual(bridge.call("b_method", 3, timeout=5), bridge._connection.call.return_value)
        bridge._connection.call.assert_called_once_with("b_method", 3, timeout=5)

        handler = lambda: None
        bridge.provide("c_method", handler)
        bridge._connection.provide.assert_called_once_with("c_method", handler)

        bridge.unprovide("c_method")
        bridge._connection.unprovide.assert_called_once_with("c_method")

        bridge.disconnect()
        bridge._connection.stop.assert_called_once()

    def test_instances_are_independent(self):
        """Each Bridge must own its own connection: no process-wide sharing."""
        first = Bridge()
        second = Bridge()
        self.assertIsNot(first._connection, second._connection)

    def test_context_manager_connects_and_disconnects(self):
        """The bridge can be used as a context manager."""
        with self.idle_conn_manager():
            bridge = Bridge()
            with bridge as entered:
                self.assertIs(entered, bridge)
                self.assertTrue(bridge._connection._read_thread.is_alive())

            self.assertTrue(bridge._connection._stop_event.is_set())
            self.assertIsNone(bridge._connection._read_thread)

    def test_garbage_collected_bridge_stops_its_connection(self):
        """An abandoned bridge must disconnect automatically when garbage collected."""
        with self.idle_conn_manager():
            bridge = Bridge()
            bridge.connect()
            connection = bridge._connection

            del bridge
            gc.collect()

            self.assertTrue(connection._stop_event.is_set())

    def test_reconnect_after_disconnect(self):
        """connect() must work again after disconnect()."""
        with self.idle_conn_manager():
            bridge = Bridge()

            bridge.connect()
            bridge.disconnect()
            self.assertTrue(bridge._connection._stop_event.is_set())

            bridge.connect()
            self.assertFalse(bridge._connection._stop_event.is_set())
            bridge.disconnect()

    def test_invalid_addresses_are_rejected(self):
        """The constructor must reject unsupported or incomplete addresses."""
        for bad_address in (
            "http://localhost:8080",  # unsupported scheme
            "/var/run/arduino-router.sock",  # missing scheme
            "tcp://localhost",  # missing port
            "tcp://:1234",  # missing host
            "unix://",  # missing path
        ):
            with self.assertRaises(ValueError, msg=f"Address '{bad_address}' was not rejected"):
                Bridge(bad_address)

    def test_unix_address_rejected_without_af_unix_support(self):
        """unix:// addresses must fail at construction on platforms lacking AF_UNIX (e.g. Windows)."""
        with patch("arduino.router_bridge.transport.socket") as mock_socket:
            del mock_socket.AF_UNIX
            with self.assertRaises(ValueError):
                Bridge("unix:///tmp/test.sock")

    def test_address_property(self):
        """The bridge exposes the address it points to."""
        bridge = Bridge("tcp://somehost:4321")
        self.assertEqual(bridge.address, "tcp://somehost:4321")
