# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import queue
import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge import DEFAULT_ADDRESS, Bridge


class UnitTest(unittest.TestCase):
    def setUp(self):
        """This method is called before each test to patch the engine dependencies."""
        self.bridges = []  # Keeps handles alive so GC finalizers don't stop engines mid-test

        # Patch dependencies
        # Mock the logger used by the engine
        self.mock_logger = MagicMock()
        self.logger_patcher = patch("arduino.router_bridge.connection.logger", self.mock_logger)
        self.logger_patcher.start()

        # Mock the socket instance that will be created
        self.mock_socket_instance = MagicMock()
        self.socket_patcher = patch("arduino.router_bridge.connection.socket")
        self.mock_socket = self.socket_patcher.start()
        self.mock_socket.socket.return_value = self.mock_socket_instance
        self.mock_socket.create_connection.return_value = self.mock_socket_instance

        # Mock only threading.Thread so the background loops never run.
        self.mock_thread_instance = MagicMock()
        self.thread_patcher = patch(
            "arduino.router_bridge.connection.threading.Thread", return_value=self.mock_thread_instance
        )
        self.mock_thread = self.thread_patcher.start()

    def tearDown(self):
        """This method is called after each test and cleans up the patched dependencies."""
        for bridge in self.bridges:
            bridge.disconnect()
        self.bridges.clear()

        self.thread_patcher.stop()
        self.socket_patcher.stop()
        self.logger_patcher.stop()

    def make_engine(self, address=DEFAULT_ADDRESS, **kwargs):
        """Creates a started engine, keeping its public handle alive for the test duration."""
        bridge = Bridge(address, **kwargs)
        bridge.connect()
        self.bridges.append(bridge)
        return bridge._engine

    def connect_client(self, client):
        """Drives the mocked connection sequence so the engine considers itself connected."""
        client._connect()
        self.assertTrue(client._is_connected_flag.is_set())

    def drain_dispatch(self, client):
        """Runs queued handler dispatches synchronously, standing in for the mocked dispatcher thread."""
        while True:
            try:
                item = client._dispatch_queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                client._run_handler(*item)
