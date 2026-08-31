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

    def test_read_loop_exits_on_unexpected_error(self):
        """The read loop must bail out on unexpected socket errors instead of spinning forever."""
        client = ClientServer()
        client._conn = MagicMock()
        client._conn.recv.side_effect = OSError("Bad file descriptor")

        client._read_loop()  # Must return, handing control back to the connection manager

        client._conn.recv.assert_called_once()  # No retry on the dead socket

    def test_read_loop_fails_pending_callbacks_on_exit(self):
        """The read loop must fail pending requests when the connection is lost."""
        client = ClientServer()
        client._conn = MagicMock()
        client._conn.recv.return_value = b""  # Orderly shutdown by the router

        on_error = MagicMock()
        client.callbacks[1] = (None, on_error)

        client._read_loop()

        on_error.assert_called_once()
        self.assertIsInstance(on_error.call_args[0][0], ConnectionError)
        self.assertEqual(len(client.callbacks), 0)

    def test_connect_clears_connected_flag_before_cleanup(self):
        """_connect must signal disconnection before tearing down a dirty connection,
        so senders never see a set flag with a broken connection."""
        client = ClientServer()
        self.connect_client(client)

        # Make the connection look broken while the flag is still set
        dirty_conn = MagicMock()
        client._conn = dirty_conn

        flag_when_closed = []
        dirty_conn.close.side_effect = lambda: flag_when_closed.append(client._is_connected_flag.is_set())

        with patch.object(client, "_is_connected", side_effect=[False, False]):
            client._connect()

        self.assertEqual(flag_when_closed, [False])


class TestIsConnected(UnitTest):
    def test_no_connection_object(self):
        """_is_connected must be False when there is no connection object."""
        client = ClientServer()
        self.assertFalse(client._is_connected())

    def test_open_idle_socket(self):
        """_is_connected must be True when the socket is open with nothing to read."""
        client = ClientServer()
        client._conn = MagicMock()
        with patch("arduino.router_bridge.connection.select.select", return_value=([], [], [])):
            self.assertTrue(client._is_connected())

    def test_peer_closed_socket(self):
        """_is_connected must be False when the peer performed an orderly shutdown."""
        client = ClientServer()
        client._conn = MagicMock()
        client._conn.recv.return_value = b""
        with patch("arduino.router_bridge.connection.select.select", return_value=([client._conn], [], [])):
            self.assertFalse(client._is_connected())

    def test_readable_socket_with_data(self):
        """_is_connected must be True when the socket has pending data."""
        client = ClientServer()
        client._conn = MagicMock()
        client._conn.recv.return_value = b"data"
        with patch("arduino.router_bridge.connection.select.select", return_value=([client._conn], [], [])):
            self.assertTrue(client._is_connected())

    def test_broken_socket(self):
        """_is_connected must be False when the socket errors out."""
        client = ClientServer()
        client._conn = MagicMock()
        with patch("arduino.router_bridge.connection.select.select", side_effect=OSError("Bad file descriptor")):
            self.assertFalse(client._is_connected())
