# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

import msgpack
from test_unit_common import UnitTest

from arduino.router_bridge import BridgeConnection
from arduino.router_bridge.bridge import ClientServer
from arduino.router_bridge.connection import GENERIC_ERR


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


class TestLockDiscipline(UnitTest):
    """Blocking I/O must never happen while holding the internal locks."""

    def test_sendall_runs_without_holding_conn_lock(self):
        """_send_bytes must not hold _conn_lock during sendall, so stop() can always shut the socket down."""
        client = ClientServer()
        self.connect_client(client)

        conn_lock_free = []

        def sendall(data):
            acquired = client._conn_lock.acquire(blocking=False)
            if acquired:
                client._conn_lock.release()
            conn_lock_free.append(acquired)

        client._conn.sendall.side_effect = sendall
        client._send_bytes(b"payload")

        self.assertEqual(conn_lock_free, [True])

    def test_cancel_request_sent_without_holding_callbacks_lock(self):
        """The timeout path must send $/cancelRequest outside callbacks_lock."""
        client = ClientServer()
        client._send_bytes = MagicMock()

        callbacks_lock_free = []

        def notify(method_name, *params):
            acquired = client.callbacks_lock.acquire(blocking=False)
            if acquired:
                client.callbacks_lock.release()
            callbacks_lock_free.append(acquired)

        client.notify = MagicMock(side_effect=notify)

        with self.assertRaises(TimeoutError):
            client.call("slow_method", timeout=0.1)

        client.notify.assert_called_once_with("$/cancelRequest", client.next_msgid)
        self.assertEqual(callbacks_lock_free, [True])

    def test_registration_on_connect_runs_without_holding_handlers_lock(self):
        """Re-registration must not hold handlers_lock across blocking $/register calls."""
        client = ClientServer()

        handlers_lock_free = []

        def call(method_name, *params, **kwargs):
            acquired = client.handlers_lock.acquire(blocking=False)
            if acquired:
                client.handlers_lock.release()
            handlers_lock_free.append(acquired)

        client.call = MagicMock(side_effect=call)
        client.provide("handler_a", lambda: None)
        client.provide("handler_b", lambda: None)

        def run_target_synchronously(target, *args, **kwargs):
            target()
            return self.mock_thread_instance

        with patch("arduino.router_bridge.connection.threading.Thread", side_effect=run_target_synchronously):
            client._connect()

        self.assertEqual(handlers_lock_free, [True, True])

    def test_msgid_reservation_skips_pending_ids(self):
        """Message IDs of pending requests must never be reused."""
        client = ClientServer()
        client.next_msgid = 0
        with client.callbacks_lock:
            client.callbacks[1] = (None, None)
            client.callbacks[2] = (None, None)
            self.assertEqual(client._next_msgid_locked(), 3)


class TestResourceLimits(UnitTest):
    def test_oversized_message_drops_the_connection(self):
        """A message exceeding max_message_size must drop the connection instead of exhausting memory."""
        client = BridgeConnection(address="unix:///tmp/test.sock", max_message_size=32)
        client._conn = MagicMock()
        client._conn.recv.return_value = msgpack.packb([2, "m", ["x" * 100]])

        client._read_loop()  # Must return so the connection manager reconnects with a fresh buffer

        client._conn.recv.assert_called_once()  # No retry after the limit was hit
        self.assertIn("exceeds", str(self.mock_logger.error.call_args))

    def test_full_handler_queue_rejects_requests(self):
        """Requests arriving with a full handler queue must be rejected as busy, not queued unboundedly."""
        client = BridgeConnection(address="unix:///tmp/test.sock", max_pending_handlers=1)
        client._send_response = MagicMock()
        client.handlers["busy_method"] = MagicMock()

        client._dispatch_queue.put_nowait("occupies the only slot")

        client._handle_msg([0, 42, "busy_method", []])

        client._send_response.assert_called_once_with(
            42, [GENERIC_ERR, "Server busy: too many pending requests."], None
        )
        client.handlers["busy_method"].assert_not_called()

    def test_full_handler_queue_drops_notifications(self):
        """Notifications arriving with a full handler queue must be dropped with a warning."""
        client = BridgeConnection(address="unix:///tmp/test.sock", max_pending_handlers=1)
        client._send_response = MagicMock()
        client.handlers["busy_method"] = MagicMock()

        client._dispatch_queue.put_nowait("occupies the only slot")

        client._handle_msg([2, "busy_method", []])

        client._send_response.assert_not_called()  # Notifications never get responses
        client.handlers["busy_method"].assert_not_called()
        self.assertIn("dropping notification", str(self.mock_logger.warning.call_args))


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
