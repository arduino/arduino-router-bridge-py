# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

from test_unit_common import UnitTest

from arduino.router_bridge.bridge import ClientServer


class TestConnection(UnitTest):
    def test_reconnect_reregisters_provided_handlers(self):
        """Tests that provided handlers are re-registered after a connection is re-established."""
        # 1. Initial connection and provide a handler.
        # The setUp method already mocks the main _conn_manager thread, so it won't run and cause errors.
        client = ClientServer()
        client.call = MagicMock()
        self.connect_client(client)

        handler = lambda: "test"
        method_name = "my_handler"
        client.provide(method_name, handler)

        client.call.assert_called_once_with("$/register", method_name)
        self.assertIn(method_name, client.handlers)
        client.call.reset_mock()

        # 2. Simulate connection loss
        client._is_connected_flag.clear()
        client._conn = None

        # 3. Trigger the reconnection logic.
        # We need to patch the threading.Thread to run the target function synchronously (register_methods_on_reconnect).
        def run_target_synchronously(target, *args, **kwargs):
            target()  # run the register_methods_on_reconnect function
            return self.mock_thread_instance

        with patch("arduino.router_bridge.connection.threading.Thread", side_effect=run_target_synchronously):
            client._connect()

        # 4. Verify that the handler was re-registered
        client.call.assert_called_once_with("$/register", method_name)

    def test_deferred_registration_happens_on_first_connection(self):
        """Tests that handlers provided while disconnected are registered on the first connection."""
        client = ClientServer()
        client.call = MagicMock()

        client.provide("early_handler", lambda: "test")
        client.call.assert_not_called()  # Not connected yet

        def run_target_synchronously(target, *args, **kwargs):
            target()
            return self.mock_thread_instance

        with patch("arduino.router_bridge.connection.threading.Thread", side_effect=run_target_synchronously):
            client._connect()

        client.call.assert_called_once_with("$/register", "early_handler")
