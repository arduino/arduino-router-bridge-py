# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock

from test_unit_common import UnitTest

from arduino.router_bridge.protocol import (
    FUNCTION_NOT_FOUND_ERR,
    GENERIC_ERR,
    MALFORMED_CALL_ERR,
    ROUTE_ALREADY_EXISTS_ERR,
)


class TestHandleMsg(UnitTest):
    """Message routing: requests and notifications go to the dispatcher, responses to pending calls."""

    def test_empty_msg(self):
        client = self.make_connection()
        client._handle_msg([])
        self.mock_logger.warning.assert_called_once_with("Invalid RPC message received (must be a non-empty list).")
        self.mock_logger.error.assert_not_called()

    def test_unknown_msg_type(self):
        client = self.make_connection()
        client._handle_msg([99, 1, None, "result"])  # Msg type 99 does not exist
        self.mock_logger.warning.assert_called_once_with("Invalid RPC message type received: 99")
        self.mock_logger.error.assert_not_called()

    def test_unknown_msg_id(self):
        client = self.make_connection()
        client._handle_msg([1, 9999, None, "result"])  # Msg id 9999 does not exist
        self.mock_logger.warning.assert_called_once_with("Response for unknown msgid 9999 received.")
        self.mock_logger.error.assert_not_called()

    def test_malformed_messages_are_logged_not_raised(self):
        """Validation errors from the protocol layer are logged and never propagate to the read loop."""
        client = self.make_connection()

        for malformed in (
            [0, 1, "method", [0, 1], "extra field"],  # Request with wrong length
            [0, 1, "method", 1],  # Request with malformed params
            [0, 123, b"method", []],  # Request with non-string method name
            [1, 1, None, "result", "extra field"],  # Response with wrong length
            [1, 1, 42, "result"],  # Response with malformed error
            [2, 1, [0, 1], "extra field"],  # Notification with wrong length
            [2, 1, 42],  # Notification with malformed params
        ):
            self.mock_logger.reset_mock()
            client._handle_msg(malformed)  # Must not raise
            self.mock_logger.warning.assert_not_called()
            self.assertIn("Message validation error", str(self.mock_logger.error.call_args))

    def test_handle_msg_request(self):
        """An incoming request runs the handler and answers with its result."""
        client = self.make_connection()
        client._send_response = MagicMock()
        client._dispatcher._send_response = client._send_response

        handler_mock = MagicMock(return_value="handled")
        client._dispatcher.add("provided_method", handler_mock)

        client._handle_msg([0, 123, "provided_method", [1, 2, 3]])
        self.drain_dispatch(client)  # Handlers run on the dispatcher thread, run synchronously here

        handler_mock.assert_called_once_with(1, 2, 3)
        client._send_response.assert_called_once_with(123, None, "handled")

    def test_handle_msg_request_handler_fail(self):
        """A handler failure answers the request with the exception type only: details must not leak."""
        client = self.make_connection()
        client._send_response = MagicMock()
        client._dispatcher._send_response = client._send_response
        client._dispatcher.add("failing_method", MagicMock(side_effect=ValueError("secret database password")))

        client._handle_msg([0, 111, "failing_method", []])
        self.drain_dispatch(client)

        client._send_response.assert_called_once()
        args, _ = client._send_response.call_args
        self.assertEqual(args[0], 111)  # msgid
        self.assertEqual(args[1], [MALFORMED_CALL_ERR, "Unhandled ValueError in handler"])  # error
        self.assertIsNone(args[2])  # result
        self.assertNotIn("secret", str(args))  # Exception details must not leak to the peer

    def test_handle_msg_request_method_not_found(self):
        client = self.make_connection()
        client._send_response = MagicMock()

        client._handle_msg([0, 456, "unknown_method", []])

        client._send_response.assert_called_once_with(
            456, [FUNCTION_NOT_FOUND_ERR, "Method not found: 'unknown_method'"], None
        )

    def test_handle_msg_notification(self):
        """An incoming notification runs the handler without sending a response."""
        client = self.make_connection()
        client._send_response = MagicMock()
        client._dispatcher._send_response = client._send_response

        handler_mock = MagicMock()
        client._dispatcher.add("notification_handler", handler_mock)

        client._handle_msg([2, "notification_handler", ["notify", "me"]])
        self.drain_dispatch(client)

        handler_mock.assert_called_once_with("notify", "me")
        client._send_response.assert_not_called()  # Notifications don't get responses

    def test_handle_msg_notification_without_handler_is_ignored(self):
        client = self.make_connection()
        client._send_response = MagicMock()

        client._handle_msg([2, "unknown_notification", []])

        client._send_response.assert_not_called()
        self.mock_logger.error.assert_not_called()

    def test_handle_msg_response(self):
        """An incoming response resolves the pending call it belongs to, exactly once."""
        client = self.make_connection()
        on_result, on_error = MagicMock(), MagicMock()
        msgid = client._pending.register(on_result, on_error)

        result_data = {"status": "ok"}
        client._handle_msg([1, msgid, None, result_data])

        on_result.assert_called_once_with(result_data)
        on_error.assert_not_called()
        self.assertIsNone(client._pending.pop(msgid))  # Consumed by the response

    def test_handle_msg_generic_error_response(self):
        client = self.make_connection()
        on_result, on_error = MagicMock(), MagicMock()
        msgid = client._pending.register(on_result, on_error)

        error = [GENERIC_ERR, "Some generic error occurred"]
        client._handle_msg([1, msgid, error, None])

        on_result.assert_not_called()
        on_error.assert_called_once_with(error)
        self.assertIsNone(client._pending.pop(msgid))

    def test_handle_msg_method_exists_error_response(self):
        """A ROUTE_ALREADY_EXISTS_ERR response is treated as success: the router already knows the method."""
        client = self.make_connection()
        on_result, on_error = MagicMock(), MagicMock()
        msgid = client._pending.register(on_result, on_error)

        client._handle_msg([1, msgid, [ROUTE_ALREADY_EXISTS_ERR, "Method already exists"], None])

        on_result.assert_called_once_with(None)
        on_error.assert_not_called()
        self.assertIsNone(client._pending.pop(msgid))
