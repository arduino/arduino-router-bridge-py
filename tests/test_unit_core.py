# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch
import msgpack

from arduino.router_bridge.bridge import ClientServer, GENERIC_ERR, set_address_resolver
from test_unit_common import UnitTest


class TestCoreFeatures(UnitTest):
    def test_initialization_default_address(self):
        """Test that the ClientServer connects to the default router address when none is given."""
        client = ClientServer()
        self.assertEqual(client.socket_type, "unix")
        self.assertEqual(client._peer_addr, "/var/run/arduino-router.sock")

    def test_address_resolver(self):
        """Test that an installed address resolver overrides the address used by new connections."""
        with patch("arduino.router_bridge.bridge._address_resolver", None):  # Remove the resolver on exit
            set_address_resolver(lambda address: "tcp://somehost:4321")
            client = ClientServer()
            self.assertEqual(client.socket_type, "tcp")
            self.assertEqual(client._peer_addr, ("somehost", 4321))

    def test_address_resolver_receives_requested_address(self):
        """Test that the resolver is given the requested address, so it can pass it through."""
        with patch("arduino.router_bridge.bridge._address_resolver", None):  # Remove the resolver on exit
            set_address_resolver(lambda address: address)
            client = ClientServer(address="unix:///tmp/test.sock")
            self.assertEqual(client._peer_addr, "/tmp/test.sock")

    def test_initialization_tcp(self):
        """Test that the ClientServer initializes correctly with a TCP address."""
        client = ClientServer(address="tcp://localhost:1234")
        self.assertEqual(client.socket_type, "tcp")
        self.assertEqual(client._peer_addr, ("localhost", 1234))
        self.mock_socket.create_connection.assert_called_with(("localhost", 1234), timeout=5)
        self.mock_thread_instance.start.assert_called_once()

    def test_initialization_unix(self):
        """Test that the ClientServer initializes correctly with a Unix socket address."""
        client = ClientServer(address="unix:///tmp/test.sock")
        self.assertEqual(client.socket_type, "unix")
        self.assertEqual(client._peer_addr, "/tmp/test.sock")
        self.mock_socket.socket.assert_called_with(self.mock_socket.AF_UNIX, self.mock_socket.SOCK_STREAM)
        self.mock_socket_instance.connect.assert_called_with("/tmp/test.sock")
        self.mock_thread_instance.start.assert_called_once()

    def test_notify(self):
        """Test that the notify method sends a correctly formatted msgpack notification."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        method_name = "test_notify"
        params = [1, "hello"]
        client.notify(method_name, *params)

        expected_request = [2, method_name, params]
        expected_packed_data = msgpack.packb(expected_request)

        client._send_bytes.assert_called_once_with(expected_packed_data)

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
        """Test providing a method and then unproviding it."""
        client = ClientServer()
        client.call = MagicMock()

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

    def test_provide_update(self):
        """Test that it is possible to update a provided method."""
        client = ClientServer()
        client.call = MagicMock()

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
