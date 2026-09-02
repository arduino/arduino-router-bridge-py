# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import time
from unittest.mock import MagicMock

import msgpack
from test_unit_common import UnitTest


class TestCoreFeatures(UnitTest):
    def respond_to_next_send(self, client, error, result):
        """Mocks _send_bytes so every sent request is answered with the given response."""

        def side_effect(data, **kwargs):
            request = msgpack.unpackb(data)
            client._handle_msg([1, request[1], error, result])  # Resolved through the real response path

        client._send_bytes = MagicMock(side_effect=side_effect)
        return client._send_bytes

    def test_initialization_default_address(self):
        """The bridge targets the default router address when none is given."""
        client = self.make_connection()
        self.assertEqual(client._socket_type, "unix")
        self.assertEqual(client._peer_addr, "/var/run/arduino-router.sock")

    def test_initialization_tcp(self):
        client = self.make_connection(address="tcp://localhost:1234")
        self.assertEqual(client._socket_type, "tcp")
        self.assertEqual(client._peer_addr, ("localhost", 1234))

    def test_notify(self):
        """notify sends a correctly formatted msgpack notification without waiting for a response."""
        client = self.make_connection()
        client._send_bytes = MagicMock()

        client.notify("test_notify", 1, "hello")

        expected_packed_data = msgpack.packb([2, "test_notify", [1, "hello"]])
        client._send_bytes.assert_called_once_with(expected_packed_data, wait_for_connection=False)

    def test_notify_does_not_block_when_disconnected(self):
        """notify returns immediately and drops the message when the router is not connected."""
        client = self.make_connection()

        start = time.monotonic()
        client.notify("set_led", "green")  # Must not wait for a reconnection
        self.assertLess(time.monotonic() - start, 0.5)

        self.assertIn("Dropped notification", str(self.mock_logger.debug.call_args))

    def test_call_successful(self):
        """A call sends a request and returns the result carried by the response."""
        client = self.make_connection()
        send = self.respond_to_next_send(client, None, "success")

        result = client.call("test_call", 42, timeout=1)

        self.assertEqual(result, "success")
        sent_request = msgpack.unpackb(send.call_args[0][0])
        self.assertEqual(sent_request, [0, sent_request[1], "test_call", [42]])

    def test_call_successful_nones(self):
        """A call without params succeeds when the response carries a None result."""
        client = self.make_connection()
        self.respond_to_next_send(client, None, None)

        self.assertIsNone(client.call("test_call", timeout=1))

    def test_call_timeout(self):
        """A call raises a TimeoutError if no response is received."""
        client = self.make_connection()
        client._send_bytes = MagicMock()  # Don't simulate a response

        with self.assertRaises(TimeoutError):
            client.call("test_timeout", timeout=0.1)

    def test_call_send_failure_raises_runtime_error(self):
        """A call whose request cannot be sent fails immediately and leaks no pending entry."""
        client = self.make_connection()
        client._send_bytes = MagicMock(side_effect=ConnectionError("Not connected to router, send failed."))

        with self.assertRaises(RuntimeError):
            client.call("test_call", timeout=1)

        self.assertIsNone(client._pending.pop(client._pending._next_msgid))

    def test_provide_and_unprovide(self):
        """Providing a method registers it with the router; unproviding unregisters it."""
        client = self.make_connection()
        client.call = MagicMock()
        self.connect_transport(client)

        handler = lambda x: x * 2

        client.provide("my_handler", handler)
        client.call.assert_called_once_with("$/register", "my_handler")
        self.assertIs(client._dispatcher.lookup("my_handler"), handler)

        client.call.reset_mock()

        client.unprovide("my_handler")
        client.call.assert_called_once_with("$/unregister", "my_handler")
        self.assertIsNone(client._dispatcher.lookup("my_handler"))

    def test_provide_while_disconnected_defers_registration(self):
        """Providing while disconnected records the handler without calling the router."""
        client = self.make_connection()
        client.call = MagicMock()

        client.provide("my_handler", lambda x: x)
        client.call.assert_not_called()  # Registration is deferred to (re)connection
        self.assertIsNotNone(client._dispatcher.lookup("my_handler"))

        client.unprovide("my_handler")
        client.call.assert_not_called()
        self.assertIsNone(client._dispatcher.lookup("my_handler"))

    def test_unprovide_unknown_method_is_a_noop(self):
        """Unproviding a method that was never provided must not call the router."""
        client = self.make_connection()
        client.call = MagicMock()
        self.connect_transport(client)

        client.unprovide("never_provided")
        client.call.assert_not_called()

    def test_provide_update(self):
        """A provided method can be updated with a new handler."""
        client = self.make_connection()
        client.call = MagicMock()
        self.connect_transport(client)

        new_handler = lambda x: x * 2
        client.provide("my_handler", lambda x: x)
        client.provide("my_handler", new_handler)

        self.assertIs(client._dispatcher.lookup("my_handler"), new_handler)
