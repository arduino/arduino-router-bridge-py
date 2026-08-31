# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge import bridge as bridge_module


class UnitTest(unittest.TestCase):
    def setUp(self):
        """This method is called before each test to reset the shared instances and patch the dependencies."""
        bridge_module._instances.clear()
        bridge_module._default_address = bridge_module.DEFAULT_ADDRESS

        # Patch dependencies
        # Mock the logger used by BridgeConnection
        self.mock_logger = MagicMock()
        self.logger_patcher = patch("arduino.router_bridge.connection.logger", self.mock_logger)
        self.logger_patcher.start()

        # Mock the socket instance that will be created
        self.mock_socket_instance = MagicMock()
        self.socket_patcher = patch("arduino.router_bridge.connection.socket")
        self.mock_socket = self.socket_patcher.start()
        self.mock_socket.socket.return_value = self.mock_socket_instance
        self.mock_socket.create_connection.return_value = self.mock_socket_instance

        # Mock only threading.Thread so the background read loop never runs.
        self.mock_thread_instance = MagicMock()
        self.thread_patcher = patch(
            "arduino.router_bridge.connection.threading.Thread", return_value=self.mock_thread_instance
        )
        self.mock_thread = self.thread_patcher.start()

    def tearDown(self):
        """This method is called after each test and cleans up the patched dependencies."""
        for instance in bridge_module._instances.values():
            instance.stop()
        bridge_module._instances.clear()
        bridge_module._default_address = bridge_module.DEFAULT_ADDRESS

        self.thread_patcher.stop()
        self.socket_patcher.stop()
        self.logger_patcher.stop()

    def connect_client(self, client):
        """Drives the mocked connection sequence so the client considers itself connected."""
        client._connect()
        self.assertTrue(client._is_connected_flag.is_set())
