# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

import msgpack
from test_unit_common import UnitTest

from arduino.router_bridge.protocol import GENERIC_ERR


class TestReconnection(UnitTest):
    def connect_with_mock_transport(self, client):
        """Runs the real _connect logic against a mocked Transport, spawned threads run inline."""
        transport = MagicMock()
        with (
            patch("arduino.router_bridge.connection.Transport") as mock_transport_cls,
            self.synchronous_threads(),
        ):
            mock_transport_cls.connect.return_value = transport
            client._connect()
        return transport

    def test_connect_publishes_transport_and_flag(self):
        client = self.make_connection()
        transport = self.connect_with_mock_transport(client)

        self.assertIs(client._transport, transport)
        self.assertTrue(client._is_connected_flag.is_set())

    def test_reconnect_reregisters_provided_handlers(self):
        """Provided handlers are re-registered after a connection is re-established."""
        client = self.make_connection()
        client.call = MagicMock()
        self.connect_transport(client)

        client.provide("my_handler", lambda: "test")
        client.call.assert_called_once_with("$/register", "my_handler")
        client.call.reset_mock()

        # Simulate connection loss, then reconnect
        client._is_connected_flag.clear()
        client._transport = None
        self.connect_with_mock_transport(client)

        client.call.assert_called_once_with("$/register", "my_handler")

    def test_deferred_registration_happens_on_first_connection(self):
        """Handlers provided while disconnected are registered on the first connection."""
        client = self.make_connection()
        client.call = MagicMock()

        client.provide("early_handler", lambda: "test")
        client.call.assert_not_called()  # Not connected yet

        self.connect_with_mock_transport(client)
        client.call.assert_called_once_with("$/register", "early_handler")

    def test_connect_reuses_a_healthy_transport(self):
        """_connect must be a no-op when the current transport is still alive."""
        client = self.make_connection()
        transport = self.connect_transport(client)
        transport.is_alive.return_value = True

        client._connect()

        self.assertIs(client._transport, transport)
        transport.close.assert_not_called()

    def test_connect_clears_connected_flag_before_cleanup(self):
        """_connect must signal disconnection before tearing down a dirty transport,
        so senders never see a set flag with a broken connection."""
        client = self.make_connection()
        dirty = self.connect_transport(client)
        dirty.is_alive.return_value = False  # Broken while the flag is still set

        flag_when_closed = []
        dirty.close.side_effect = lambda: flag_when_closed.append(client._is_connected_flag.is_set())

        client._stop_event.set()  # Skip the reconnect loop: only the cleanup is under test
        client._connect()

        self.assertEqual(flag_when_closed, [False])

    def test_connect_undoes_publication_when_stopped_concurrently(self):
        """stop() racing _connect must leave the connection torn down, not half-published."""
        client = self.make_connection()
        transport = MagicMock()
        with patch("arduino.router_bridge.connection.Transport") as mock_transport_cls:
            # stop() runs right after the connection is established, before it is published
            mock_transport_cls.connect.side_effect = lambda *args: (client._stop_event.set(), transport)[1]
            client._connect()

        self.assertIsNone(client._transport)
        self.assertFalse(client._is_connected_flag.is_set())
        transport.close.assert_called_once()

    def test_connect_retries_until_stopped(self):
        """Connection failures are logged and retried, and stop() breaks the retry loop."""
        client = self.make_connection()
        attempts = []

        def failing_connect(*args):
            attempts.append(1)
            if len(attempts) == 3:
                client._stop_event.set()
            raise OSError("Connection refused")

        with (
            patch("arduino.router_bridge.connection.Transport") as mock_transport_cls,
            patch("arduino.router_bridge.connection._reconnect_delay", 0),
        ):
            mock_transport_cls.connect.side_effect = failing_connect
            client._connect()

        self.assertEqual(len(attempts), 3)
        self.assertIn("Failed to connect to router", str(self.mock_logger.error.call_args))


class TestReadLoop(UnitTest):
    def test_read_loop_exits_on_unexpected_error(self):
        """The read loop must bail out on unexpected socket errors instead of spinning forever."""
        client = self.make_connection()
        transport = self.connect_transport(client)
        transport.recv.side_effect = OSError("Bad file descriptor")

        client._read_loop()  # Must return, handing control back to the connection manager

        transport.recv.assert_called_once()  # No retry on the dead socket
        self.assertIsNone(client._transport)  # Dropped so _connect rebuilds instead of reusing it
        transport.close.assert_called_once()

    def test_read_loop_fails_pending_callbacks_on_exit(self):
        """The read loop must fail pending requests when the connection is lost."""
        client = self.make_connection()
        transport = self.connect_transport(client)
        transport.recv.return_value = b""  # Orderly shutdown by the router

        on_error = MagicMock()
        msgid = client._pending.register(None, on_error)

        client._read_loop()

        on_error.assert_called_once()
        self.assertIsInstance(on_error.call_args[0][0], ConnectionError)
        self.assertIsNone(client._pending.pop(msgid))  # Consumed by the failure

    def test_read_loop_dispatches_incoming_messages(self):
        """Messages arriving on the socket reach _handle_msg via the streaming unpacker."""
        client = self.make_connection()
        transport = self.connect_transport(client)
        transport.recv.side_effect = [msgpack.packb([2, "my_notification", ["x"]]), b""]

        handler = MagicMock()
        client._dispatcher.add("my_notification", handler)

        client._read_loop()
        self.drain_dispatch(client)

        handler.assert_called_once_with("x")

    def test_oversized_message_drops_the_connection(self):
        """A message exceeding max_message_size must drop the connection instead of exhausting memory."""
        client = self.make_connection(address="unix:///tmp/test.sock", max_message_size=32)
        transport = self.connect_transport(client)
        transport.recv.return_value = msgpack.packb([2, "m", ["x" * 100]])

        client._read_loop()  # Must return so the connection manager reconnects with a fresh buffer

        transport.recv.assert_called_once()  # No retry after the limit was hit
        self.assertIn("exceeds", str(self.mock_logger.error.call_args))
        self.assertIsNone(client._transport)  # Oversized message drops the socket so the stream can resync


class TestLockDiscipline(UnitTest):
    """Blocking I/O must never happen while holding the internal locks."""

    def test_send_runs_without_holding_transport_lock(self):
        """_send_bytes must not hold _transport_lock during the write, so stop() can always shut the socket down."""
        client = self.make_connection()
        transport = self.connect_transport(client)

        transport_lock_free = []

        def send_all(data):
            acquired = client._transport_lock.acquire(blocking=False)
            if acquired:
                client._transport_lock.release()
            transport_lock_free.append(acquired)

        transport.send_all.side_effect = send_all
        client._send_bytes(b"payload")

        self.assertEqual(transport_lock_free, [True])

    def test_send_stall_drops_connection(self):
        """A send that stalls past the timeout must fail and drop the transport, not stay wedged."""
        client = self.make_connection()
        transport = self.connect_transport(client)
        transport.send_all.side_effect = TimeoutError("Send stalled for 15.0s")

        with self.assertRaises(ConnectionError):
            client._send_bytes(b"payload")

        self.assertIsNone(client._transport)  # Dropped so _conn_manager reconnects and resyncs
        transport.close.assert_called_once()

    def test_cancel_request_sent_without_holding_pending_lock(self):
        """The timeout path must send $/cancelRequest outside the pending-calls lock."""
        client = self.make_connection()
        client._send_bytes = MagicMock()

        pending_lock_free = []

        def notify(method_name, *params):
            acquired = client._pending._lock.acquire(blocking=False)
            if acquired:
                client._pending._lock.release()
            pending_lock_free.append(acquired)

        client.notify = MagicMock(side_effect=notify)

        with self.assertRaises(TimeoutError):
            client.call("slow_method", timeout=0.1)

        client.notify.assert_called_once_with("$/cancelRequest", client._pending._next_msgid)
        self.assertEqual(pending_lock_free, [True])

    def test_registration_on_connect_runs_without_holding_handlers_lock(self):
        """Re-registration must not hold the handlers lock across blocking $/register calls."""
        client = self.make_connection()

        handlers_lock_free = []

        def call(method_name, *params, **kwargs):
            acquired = client._dispatcher._handlers_lock.acquire(blocking=False)
            if acquired:
                client._dispatcher._handlers_lock.release()
            handlers_lock_free.append(acquired)

        client.call = MagicMock(side_effect=call)
        client.provide("handler_a", lambda: None)
        client.provide("handler_b", lambda: None)

        with self.synchronous_threads():
            client._register_provided_methods()

        self.assertEqual(handlers_lock_free, [True, True])


class TestNestedCallGuard(UnitTest):
    """Handlers run on the dispatcher thread and must not perform nested bridge calls."""

    def test_call_from_dispatcher_thread_is_rejected(self):
        client = self.make_connection()
        client._send_bytes = MagicMock()
        self.mark_dispatch_thread(client)

        with self.assertRaises(RuntimeError) as cm:
            client.call("nested_method")

        self.assertIn("nested bridge calls are not supported", str(cm.exception))
        client._send_bytes.assert_not_called()  # Rejected before anything reaches the wire
        self.assertIsNone(client._pending.pop(1))  # No pending entry leaks

    def test_notify_from_dispatcher_thread_is_allowed(self):
        client = self.make_connection()
        client._send_bytes = MagicMock()
        self.mark_dispatch_thread(client)

        client.notify("progress", 42)

        client._send_bytes.assert_called_once()

    def test_handler_nested_call_is_reported_to_peer(self):
        """A handler attempting a nested call must fail and answer the request with an error."""
        client = self.make_connection()
        client._send_response = MagicMock()
        client._dispatcher._send_response = client._send_response
        client._dispatcher.add("nested", lambda: client.call("other_method"))
        self.mark_dispatch_thread(client)  # drain_dispatch runs handlers on this thread

        client._handle_msg([0, 5, "nested", []])
        self.drain_dispatch(client)

        client._send_response.assert_called_once_with(5, [GENERIC_ERR, "Unhandled RuntimeError in handler"], None)

    def test_provide_from_dispatcher_thread_registers_in_background(self):
        """provide() from a handler must not block on the registration call."""
        client = self.make_connection()
        client.call = MagicMock()
        self.connect_transport(client)
        self.mark_dispatch_thread(client)

        with patch("arduino.router_bridge.connection.threading.Thread") as mock_thread:
            client.provide("from_handler", lambda: None)

        self.assertIsNotNone(client._dispatcher.lookup("from_handler"))
        client.call.assert_not_called()  # Registration must not run inline on the dispatcher thread
        _, thread_kwargs = mock_thread.call_args
        self.assertEqual(thread_kwargs.get("name"), "Bridge.registration")


class TestResourceLimits(UnitTest):
    def test_full_handler_queue_rejects_requests(self):
        """Requests arriving with a full handler queue must be rejected as busy, not queued unboundedly."""
        client = self.make_connection(address="unix:///tmp/test.sock", max_pending_handlers=1)
        client._send_response = MagicMock()
        busy_handler = MagicMock()
        client._dispatcher.add("busy_method", busy_handler)

        client._dispatcher.submit(lambda: None, "filler", None, [])  # Occupies the only slot

        client._handle_msg([0, 42, "busy_method", []])

        client._send_response.assert_called_once_with(
            42, [GENERIC_ERR, "Server busy: too many pending requests."], None
        )
        busy_handler.assert_not_called()

    def test_full_handler_queue_drops_notifications(self):
        """Notifications arriving with a full handler queue must be dropped with a warning."""
        client = self.make_connection(address="unix:///tmp/test.sock", max_pending_handlers=1)
        client._send_response = MagicMock()
        busy_handler = MagicMock()
        client._dispatcher.add("busy_method", busy_handler)

        client._dispatcher.submit(lambda: None, "filler", None, [])  # Occupies the only slot

        client._handle_msg([2, "busy_method", []])

        client._send_response.assert_not_called()  # Notifications never get responses
        busy_handler.assert_not_called()
        self.assertIn("dropping notification", str(self.mock_logger.warning.call_args))
