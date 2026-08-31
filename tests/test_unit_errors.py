# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock

from test_unit_common import UnitTest

from arduino.router_bridge.bridge import ClientServer
from arduino.router_bridge.connection import BUFFER_LIMIT_EXCEEDED_ERR, GENERIC_ERR


class TestErrors(UnitTest):
    def test_connection_lost(self):
        """Test that pending callbacks fail and are cleaned up when connection is lost."""
        client = ClientServer()

        on_error_1 = MagicMock()
        on_error_2 = MagicMock()

        client.callbacks[1] = (None, on_error_1)
        client.callbacks[2] = (None, on_error_2)

        reason = ConnectionError("Connection to router lost.")
        client._fail_pending_callbacks(reason)  # This call is triggered by ConnectionResetError

        on_error_1.assert_called_once_with(reason)
        on_error_2.assert_called_once_with(reason)
        self.assertEqual(len(client.callbacks), 0)

    def test_call_pending_during_connection_loss_raises_connection_error(self):
        """Test that a call pending while the connection drops raises a ConnectionError."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        def side_effect(*args, **kwargs):
            # Simulate the connection dropping right after the request is sent
            client._fail_pending_callbacks(ConnectionError("Connection to router lost."))

        client._send_bytes.side_effect = side_effect

        with self.assertRaises(ConnectionError) as cm:
            client.call("test_method", timeout=1)

        self.assertIn("Connection to router lost", str(cm.exception))
        self.assertEqual(len(client.callbacks), 0)

    def test_call_pending_during_stop_raises_connection_error(self):
        """Test that a call pending while the bridge is stopped raises a ConnectionError."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        def side_effect(*args, **kwargs):
            client.stop()  # stop() fails all pending callbacks

        client._send_bytes.side_effect = side_effect

        with self.assertRaises(ConnectionError) as cm:
            client.call("test_method", timeout=1)

        self.assertIn("stopped", str(cm.exception))

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

    def test_provide_error(self):
        """Test that providing a non-callable handler raises a ValueError."""
        client = ClientServer()
        with self.assertRaises(ValueError):
            client.provide("bad_handler", "not a function")

    def test_clear_callbacks_after_connection_lost(self):
        """Test that pending callbacks are correctly failed when the connection is lost."""
        client = ClientServer()

        on_error_1 = MagicMock()
        on_error_2 = MagicMock()

        client.callbacks[1] = (None, on_error_1)
        client.callbacks[2] = (None, on_error_2)

        reason = ConnectionError("Connection to router lost.")
        client._fail_pending_callbacks(reason)  # This call is triggered by ConnectionResetError in _read_loop

        on_error_1.assert_called_once_with(reason)
        on_error_2.assert_called_once_with(reason)
        self.assertEqual(len(client.callbacks), 0)

    def test_buffer_limit_error_propagates(self):
        """Test that a BUFFER_LIMIT_EXCEEDED_ERR response is propagated to the registered callback."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        method_name = "test_buffer_limit"
        error_response = [BUFFER_LIMIT_EXCEEDED_ERR, "message size exceeds limit of 128 bytes"]
        msgid = client.next_msgid + 1

        def side_effect(*args, **kwargs):
            _, on_error = client.callbacks[msgid]
            on_error(error_response)

        client._send_bytes.side_effect = side_effect

        with self.assertRaises(ValueError) as cm:
            client.call(method_name)

        self.assertIn("message size exceeds limit", str(cm.exception))
