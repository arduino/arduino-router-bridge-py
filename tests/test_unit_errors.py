# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock

import msgpack
from test_unit_common import UnitTest

from arduino.router_bridge.protocol import BUFFER_LIMIT_EXCEEDED_ERR, GENERIC_ERR


class TestErrors(UnitTest):
    def respond_to_next_send(self, client, error, result):
        """Mocks _send_bytes so every sent request is answered with the given response."""

        def side_effect(data, **kwargs):
            request = msgpack.unpackb(data)
            client._handle_msg([1, request[1], error, result])

        client._send_bytes = MagicMock(side_effect=side_effect)

    def test_call_pending_during_connection_loss_raises_connection_error(self):
        """A call pending while the connection drops raises a ConnectionError."""
        client = self.make_connection()

        # Simulate the connection dropping right after the request is sent
        client._send_bytes = MagicMock(
            side_effect=lambda *a, **kw: client._pending.fail_all(ConnectionError("Connection to router lost."))
        )

        with self.assertRaises(ConnectionError) as cm:
            client.call("test_method", timeout=1)

        self.assertIn("Connection to router lost", str(cm.exception))

    def test_call_pending_during_stop_raises_connection_error(self):
        """A call pending while the bridge is stopped raises a ConnectionError."""
        client = self.make_connection()
        client._send_bytes = MagicMock(side_effect=lambda *a, **kw: client.stop())  # stop() fails pending callbacks

        with self.assertRaises(ConnectionError) as cm:
            client.call("test_method", timeout=1)

        self.assertIn("stopped", str(cm.exception))

    def test_call_server_error(self):
        """A call that receives an error response raises a ValueError carrying the router's message."""
        client = self.make_connection()
        self.respond_to_next_send(client, [GENERIC_ERR, "Something went wrong"], None)

        with self.assertRaises(ValueError) as cm:
            client.call("test_error")

        self.assertIn("Something went wrong", str(cm.exception))

    def test_buffer_limit_error_propagates(self):
        """A BUFFER_LIMIT_EXCEEDED_ERR response from the router is propagated to the caller."""
        client = self.make_connection()
        self.respond_to_next_send(client, [BUFFER_LIMIT_EXCEEDED_ERR, "message size exceeds limit of 128 bytes"], None)

        with self.assertRaises(ValueError) as cm:
            client.call("test_buffer_limit")

        self.assertIn("message size exceeds limit", str(cm.exception))

    def test_provide_error(self):
        """Providing a non-callable handler raises a ValueError."""
        client = self.make_connection()
        with self.assertRaises(ValueError):
            client.provide("bad_handler", "not a function")
