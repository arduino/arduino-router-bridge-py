# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import time
from unittest.mock import MagicMock

import msgpack
from test_unit_common import UnitTest

from arduino.router_bridge import GENERIC_ERR, Bridge, ClientServer


class TestCoreFeatures(UnitTest):
    def test_initialization_default_address(self):
        """Test that the ClientServer connects to the default router address when none is given."""
        client = ClientServer()
        self.assertEqual(client.socket_type, "unix")
        self.assertEqual(client._peer_addr, "/var/run/arduino-router.sock")

    def test_bridge_connect_binds_the_default_address(self):
        """Test that Bridge.connect changes the address used when none is given."""
        client = Bridge.connect("tcp://somehost:4321")
        self.assertEqual(client.socket_type, "tcp")
        self.assertEqual(client._peer_addr, ("somehost", 4321))
        self.assertIs(ClientServer(), client)  # Unaddressed lookups now use the bound connection

    def test_explicit_address_wins_over_the_bound_default(self):
        """Test that an explicit address is honored even after Bridge.connect."""
        Bridge.connect("tcp://somehost:4321")
        client = ClientServer(address="unix:///tmp/test.sock")
        self.assertEqual(client._peer_addr, "/tmp/test.sock")

    def test_initialization_tcp(self):
        """Test that the ClientServer initializes correctly with a TCP address and connects on demand."""
        client = ClientServer(address="tcp://localhost:1234")
        self.assertEqual(client.socket_type, "tcp")
        self.assertEqual(client._peer_addr, ("localhost", 1234))
        self.assertEqual(self.mock_thread_instance.start.call_count, 2)  # start() spawns read loop and dispatcher

        self.connect_client(client)
        self.mock_socket.create_connection.assert_called_with(("localhost", 1234), timeout=5)

    def test_initialization_unix(self):
        """Test that the ClientServer initializes correctly with a Unix socket address and connects on demand."""
        client = ClientServer(address="unix:///tmp/test.sock")
        self.assertEqual(client.socket_type, "unix")
        self.assertEqual(client._peer_addr, "/tmp/test.sock")
        self.assertEqual(self.mock_thread_instance.start.call_count, 2)  # start() spawns read loop and dispatcher

        self.connect_client(client)
        self.mock_socket.socket.assert_called_with(self.mock_socket.AF_UNIX, self.mock_socket.SOCK_STREAM)
        self.mock_socket_instance.connect.assert_called_with("/tmp/test.sock")

    def test_notify(self):
        """Test that the notify method sends a correctly formatted msgpack notification."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        method_name = "test_notify"
        params = [1, "hello"]
        client.notify(method_name, *params)

        expected_request = [2, method_name, params]
        expected_packed_data = msgpack.packb(expected_request)

        client._send_bytes.assert_called_once_with(expected_packed_data, wait_for_connection=False)

    def test_notify_does_not_block_when_disconnected(self):
        """Test that notify returns immediately and drops the message when the router is not connected."""
        client = ClientServer()

        start = time.monotonic()
        client.notify("set_led", "green")  # Must not wait for a reconnection
        self.assertLess(time.monotonic() - start, 0.5)

        self.mock_socket_instance.sendall.assert_not_called()

    def test_call_successful(self):
        """Test a successful RPC call where a response is received."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        method_name = "test_call"
        params = [42]
        expected_result = "success"
        msgid = client.next_msgid + 1

        # Simulate the response handling part
        def side_effect(*args, **kwargs):
            # The call method will add a callback. We can invoke it to simulate a response.
            on_result, _ = client.callbacks[msgid]
            on_result(expected_result)

        client._send_bytes.side_effect = side_effect

        result = client.call(method_name, *params, timeout=1)

        expected_request = [0, msgid, method_name, params]
        client._send_bytes.assert_called_once_with(msgpack.packb(expected_request))
        self.assertEqual(result, expected_result)

    def test_call_successful_nones(self):
        """Test a successful RPC call without params where a None response is received."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        method_name = "test_call"
        params = ()
        expected_result = None
        msgid = client.next_msgid + 1

        # Simulate the response handling part
        def side_effect(*args, **kwargs):
            # The call method will add a callback. We can invoke it to simulate a response.
            on_result, _ = client.callbacks[msgid]
            on_result(expected_result)

        client._send_bytes.side_effect = side_effect

        result = client.call(method_name, *params, timeout=1)

        expected_request = [0, msgid, method_name, params]
        client._send_bytes.assert_called_once_with(msgpack.packb(expected_request))
        self.assertEqual(result, expected_result)

    def test_call_timeout(self):
        """Test that an RPC call raises a TimeoutError if no response is received."""
        client = ClientServer()
        client._send_bytes = MagicMock()  # Don't simulate a response

        with self.assertRaises(TimeoutError):
            client.call("test_timeout", timeout=0.1)

    def test_call_server_error(self):
        """Test an RPC call that returns an error from the server."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        method_name = "test_error"
        error_response = [GENERIC_ERR, "Something went wrong"]
        msgid = client.next_msgid + 1

        def side_effect(*args, **kwargs):
            _, on_error = client.callbacks[msgid]
            on_error(error_response)

        client._send_bytes.side_effect = side_effect

        with self.assertRaises(ValueError) as cm:
            client.call(method_name)

        self.assertIn("Something went wrong", str(cm.exception))

    def test_provide_and_unprovide(self):
        """Test providing a method and then unproviding it while connected."""
        client = ClientServer()
        client.call = MagicMock()
        self.connect_client(client)

        method_name = "my_handler"
        handler = lambda x: x * 2

        # Test provide
        client.provide(method_name, handler)
        client.call.assert_called_once_with("$/register", method_name)
        self.assertIn(method_name, client.handlers)
        self.assertEqual(client.handlers[method_name], handler)

        client.call.reset_mock()

        # Test unprovide
        client.unprovide(method_name)
        client.call.assert_called_once_with("$/unregister", method_name)
        self.assertNotIn(method_name, client.handlers)

    def test_provide_while_disconnected_defers_registration(self):
        """Test that providing while disconnected records the handler without calling the router."""
        client = ClientServer()
        client.call = MagicMock()

        method_name = "my_handler"
        handler = lambda x: x

        client.provide(method_name, handler)
        client.call.assert_not_called()  # Registration is deferred to (re)connection
        self.assertIn(method_name, client.handlers)

        client.unprovide(method_name)
        client.call.assert_not_called()
        self.assertNotIn(method_name, client.handlers)

    def test_provide_update(self):
        """Test that it is possible to update a provided method."""
        client = ClientServer()
        client.call = MagicMock()
        self.connect_client(client)

        method_name = "my_handler"
        handler = lambda x: x
        new_handler = lambda x: x * 2

        client.provide(method_name, handler)
        client.call.assert_called_once_with("$/register", method_name)
        self.assertIn(method_name, client.handlers)
        self.assertEqual(client.handlers[method_name], handler)

        client.call.reset_mock()

        client.provide(method_name, new_handler)
        client.call.assert_called_once_with("$/register", method_name)
        self.assertIn(method_name, client.handlers)
        self.assertEqual(client.handlers[method_name], new_handler)
