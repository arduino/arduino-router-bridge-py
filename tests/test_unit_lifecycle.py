# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import gc
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge import Bridge
from arduino.router_bridge.connection import _BridgeConnection


class TestLifecycle(unittest.TestCase):
    """Lifecycle tests for the bridge, using real threads."""

    def setUp(self):
        for module in ("connection", "dispatch", "transport"):
            patcher = patch(f"arduino.router_bridge.{module}.logger", MagicMock())
            patcher.start()
            self.addCleanup(patcher.stop)

    def _start_dummy_server(self, accept_count=1):
        """Starts a Unix-socket server that accepts the given number of connections and holds them open."""
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        sock_path = os.path.join(tmpdir.name, "test.sock")

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(accept_count)
        self.addCleanup(server_sock.close)

        ready = threading.Event()

        def serve():
            ready.set()
            for _ in range(accept_count):
                try:
                    conn, _ = server_sock.accept()
                    self.addCleanup(conn.close)
                except OSError:
                    return

        threading.Thread(target=serve, daemon=True).start()
        self.assertTrue(ready.wait(timeout=2), "Dummy server did not become ready")
        return sock_path

    def test_connect_is_non_blocking(self):
        """connect() must return immediately even if the router is not reachable."""
        bridge = Bridge("unix:///tmp/never-exists.sock")
        bridge.connect()  # Must not block waiting for a connection
        self.addCleanup(bridge.disconnect)
        self.assertFalse(bridge.wait_connected(timeout=0.1))

    def test_connect_then_disconnect_joins_background_threads(self):
        """connect() spawns real background threads; disconnect() must join them so they do not leak."""
        sock_path = self._start_dummy_server()

        bridge = Bridge(f"unix://{sock_path}")
        bridge.connect()
        self.assertTrue(bridge.wait_connected(timeout=2), "Bridge did not connect")
        read_thread = bridge._connection._read_thread
        dispatch_thread = bridge._connection._dispatcher._thread
        self.assertTrue(read_thread.is_alive())
        self.assertTrue(dispatch_thread.is_alive())

        bridge.disconnect()

        self.assertFalse(read_thread.is_alive(), "Read thread leaked after disconnect()")
        self.assertFalse(dispatch_thread.is_alive(), "Dispatcher thread leaked after disconnect()")
        self.assertIsNone(bridge._connection._read_thread)

    def test_abandoned_handle_stops_connection_without_blocking(self):
        """Dropping a handle must stop its connection via the non-blocking finalizer, not a join in GC."""
        sock_path = self._start_dummy_server()

        bridge = Bridge(f"unix://{sock_path}")
        bridge.connect()
        self.assertTrue(bridge.wait_connected(timeout=2), "Bridge did not connect")
        connection = bridge._connection  # Outlives the handle so we can observe the wind-down
        read_thread = connection._read_thread
        dispatch_thread = connection._dispatcher._thread

        del bridge
        gc.collect()  # Fires the finalizer; must not block on a thread join

        # Daemon threads notice the stop event and wind down on their own
        read_thread.join(timeout=2)
        dispatch_thread.join(timeout=2)
        self.assertFalse(read_thread.is_alive(), "Read thread leaked after handle was collected")
        self.assertFalse(dispatch_thread.is_alive(), "Dispatcher thread leaked after handle was collected")
        self.assertTrue(connection._stop_event.is_set())

    def test_disconnect_without_connect_is_safe(self):
        """disconnect() must be a safe no-op even if connect() was never called."""
        bridge = Bridge("unix:///tmp/never-exists.sock")
        bridge.disconnect()  # Must not raise or block
        self.assertIsNone(bridge._connection._read_thread)

    def test_context_manager_connects_and_disconnects(self):
        """The bridge can be used as a context manager that connects on enter and disconnects on exit."""
        bridge = Bridge("unix:///tmp/never-exists.sock")

        # Avoid real connecting/looping
        with (
            patch.object(_BridgeConnection, "_connect"),
            patch.object(_BridgeConnection, "_conn_manager", lambda self: self._stop_event.wait()),
        ):
            with bridge as entered:
                self.assertIs(entered, bridge)
                self.assertTrue(bridge._connection._read_thread.is_alive())

        self.assertTrue(bridge._connection._stop_event.is_set())
        read_thread = bridge._connection._read_thread
        self.assertFalse(read_thread is not None and read_thread.is_alive())

    def test_connect_is_idempotent(self):
        """Calling connect() twice does not spawn a second set of background threads."""
        with (
            patch.object(_BridgeConnection, "_connect"),
            patch.object(_BridgeConnection, "_conn_manager", lambda self: self._stop_event.wait()),
        ):
            bridge = Bridge("unix:///tmp/never-exists.sock")
            bridge.connect()
            first_thread = bridge._connection._read_thread
            bridge.connect()  # idempotent
            self.assertIs(bridge._connection._read_thread, first_thread)
            bridge.disconnect()

    def test_reconnect_after_disconnect(self):
        """A disconnected bridge can connect again and reach the router from scratch."""
        sock_path = self._start_dummy_server(accept_count=2)

        bridge = Bridge(f"unix://{sock_path}")
        self.addCleanup(bridge.disconnect)

        bridge.connect()
        self.assertTrue(bridge.wait_connected(timeout=2), "Bridge did not connect")
        bridge.disconnect()
        self.assertFalse(bridge.wait_connected(timeout=0.1))

        bridge.connect()
        self.assertTrue(bridge.wait_connected(timeout=2), "Bridge did not reconnect after restart")
