# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

from test_unit_common import UnitTest

from arduino.router_bridge.protocol import METHOD_NOT_AVAILABLE_ERR, ROUTE_ALREADY_EXISTS_ERR, RpcError

ROUTE_EXISTS = [ROUTE_ALREADY_EXISTS_ERR, "route already exists: dup"]
NO_UNREGISTER = [METHOD_NOT_AVAILABLE_ERR, "method $/unregister not available"]


class TestRegistration(UnitTest):
    """How provide/unprovide react to the router's answers to $/register and $/unregister."""

    def make_connected(self):
        client = self.make_connection()
        self.connect_transport(client)
        return client

    def reconnect(self, client):
        """Runs the real _connect logic against a mocked Transport, so re-registration happens inline."""
        with (
            patch("arduino.router_bridge.connection.Transport") as mock_transport_cls,
            self.synchronous_threads(),
        ):
            mock_transport_cls.connect.return_value = MagicMock()
            client._connect()

    def test_provide_conflicting_with_another_client_fails(self):
        """Providing a method another client owns is a programming error: it fails and the handler is dropped."""
        client = self.make_connected()
        self.answer_requests(client, ROUTE_EXISTS, None)

        with self.assertRaises(ValueError) as cm:
            client.provide("dup", lambda: "B")

        self.assertIn("'dup'", str(cm.exception))
        self.assertIsNone(client._dispatcher.lookup("dup"))
        client._send_bytes.reset_mock()
        self.reconnect(client)
        client._send_bytes.assert_not_called()  # Never registered again

    def test_conflict_from_a_handler_is_logged(self):
        """From a handler the registration runs in the background, so the conflict can only be logged."""
        client = self.make_connected()
        # Inline threads would run on the marked dispatcher thread, where call() is rejected: stub the answer instead
        client.call = MagicMock(side_effect=RpcError("$/register", *ROUTE_EXISTS))
        self.mark_dispatch_thread(client)

        with self.synchronous_threads():
            client.provide("dup", lambda: "B")  # Must not raise on the dispatcher thread

        self.mock_logger.error.assert_called_once()
        self.assertIn("'dup'", self.mock_logger.error.call_args.args[0])
        self.assertIsNone(client._dispatcher.lookup("dup"))

    def test_reproviding_an_owned_method_is_silent(self):
        """Replacing the handler of a method this connection already registered is not a conflict."""
        client = self.make_connected()
        self.answer_requests(client, None, True)
        client.provide("dup", lambda: "A")

        self.answer_requests(client, ROUTE_EXISTS, None)
        client.provide("dup", lambda: "B")

        self.mock_logger.warning.assert_not_called()
        self.mock_logger.error.assert_not_called()

    def test_unprovide_on_a_router_without_unregister_is_quiet(self):
        """The router may lack $/unregister: the route stays bound here, which is not an error."""
        client = self.make_connected()
        self.answer_requests(client, None, True)
        client.provide("dup", lambda: "A")

        self.answer_requests(client, NO_UNREGISTER, None)
        client.unprovide("dup")

        self.assertIsNone(client._dispatcher.lookup("dup"))
        self.mock_logger.error.assert_not_called()
        self.mock_logger.warning.assert_not_called()
        self.mock_logger.debug.assert_not_called()  # Expected on older routers: nothing to say

    def test_reproviding_after_unprovide_on_a_router_without_unregister_is_silent(self):
        """Since the route stayed bound to this connection, the router's "route already exists" is harmless."""
        client = self.make_connected()
        self.answer_requests(client, None, True)
        client.provide("dup", lambda: "A")
        self.answer_requests(client, NO_UNREGISTER, None)
        client.unprovide("dup")

        self.answer_requests(client, ROUTE_EXISTS, None)
        client.provide("dup", lambda: "B")

        self.mock_logger.warning.assert_not_called()
        self.mock_logger.error.assert_not_called()

    def test_conflict_on_reconnection_is_logged_and_drops_the_handler(self):
        """A new connection owns nothing: a conflict while re-registering is logged and the handler dropped."""
        client = self.make_connected()
        self.answer_requests(client, None, True)
        client.provide("dup", lambda: "A")

        self.answer_requests(client, ROUTE_EXISTS, None)
        self.reconnect(client)

        self.mock_logger.error.assert_called_once()
        self.assertIn("'dup'", self.mock_logger.error.call_args.args[0])
        self.assertIsNone(client._dispatcher.lookup("dup"))

    def test_other_registration_failures_are_errors(self):
        client = self.make_connected()
        self.answer_requests(client, [1, "invalid params"], None)

        handler = lambda: "A"  # noqa: E731
        client.provide("dup", handler)
        self.assertIs(client._dispatcher.lookup("dup"), handler)  # Kept: re-registered on reconnection
        client.unprovide("dup")

        self.assertEqual(self.mock_logger.error.call_count, 2)
