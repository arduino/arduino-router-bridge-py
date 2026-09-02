# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge.dispatch import Dispatcher
from arduino.router_bridge.protocol import GENERIC_ERR, MALFORMED_CALL_ERR


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.mock_logger = MagicMock()
        logger_patcher = patch("arduino.router_bridge.dispatch.logger", self.mock_logger)
        logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        self.send_response = MagicMock()
        self.dispatcher = Dispatcher(max_pending=4, send_response=self.send_response)

    def run_queued(self):
        """Runs queued handler executions synchronously, standing in for the dispatcher thread."""
        while True:
            try:
                item = self.dispatcher._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self.dispatcher._run_handler(*item)


class TestHandlerRegistry(DispatcherTest):
    def test_add_lookup_remove(self):
        handler = lambda: "ok"
        self.dispatcher.add("my_method", handler)
        self.assertIs(self.dispatcher.lookup("my_method"), handler)
        self.assertEqual(self.dispatcher.method_names(), ["my_method"])

        self.assertIs(self.dispatcher.remove("my_method"), handler)
        self.assertIsNone(self.dispatcher.lookup("my_method"))
        self.assertEqual(self.dispatcher.method_names(), [])

    def test_add_replaces_previous_handler(self):
        first, second = lambda: 1, lambda: 2
        self.dispatcher.add("my_method", first)
        self.dispatcher.add("my_method", second)
        self.assertIs(self.dispatcher.lookup("my_method"), second)

    def test_add_rejects_non_callable(self):
        with self.assertRaises(ValueError):
            self.dispatcher.add("bad_handler", "not a function")

    def test_remove_unknown_returns_none(self):
        self.assertIsNone(self.dispatcher.remove("never_added"))


class TestSubmit(DispatcherTest):
    def test_submit_queues_execution(self):
        handler = MagicMock(return_value="handled")
        self.assertTrue(self.dispatcher.submit(handler, "my_method", 7, [1, 2]))

        self.run_queued()

        handler.assert_called_once_with(1, 2)
        self.send_response.assert_called_once_with(7, None, "handled")

    def test_submit_reports_full_queue(self):
        dispatcher = Dispatcher(max_pending=1, send_response=self.send_response)
        self.assertTrue(dispatcher.submit(lambda: None, "a", None, []))
        self.assertFalse(dispatcher.submit(lambda: None, "b", None, []))  # Bounded: no unbounded growth


class TestRunHandler(DispatcherTest):
    def test_notification_gets_no_response(self):
        """msgid None marks a notification: the handler runs but nothing is sent back."""
        handler = MagicMock()
        self.dispatcher.submit(handler, "my_method", None, ["x"])
        self.run_queued()

        handler.assert_called_once_with("x")
        self.send_response.assert_not_called()

    def test_handler_exception_maps_to_error_codes(self):
        """TypeError/ValueError map to MALFORMED_CALL_ERR, anything else to GENERIC_ERR."""
        for exception, err_code in (
            (ValueError("bad input"), MALFORMED_CALL_ERR),
            (TypeError("bad type"), MALFORMED_CALL_ERR),
            (RuntimeError("boom"), GENERIC_ERR),
        ):
            self.send_response.reset_mock()
            self.dispatcher.submit(MagicMock(side_effect=exception), "failing", 7, [])
            self.run_queued()
            self.send_response.assert_called_once_with(
                7, [err_code, f"Unhandled {type(exception).__name__} in handler"], None
            )

    def test_handler_exception_details_do_not_leak_to_peer(self):
        """Exception details stay in the local log; the peer only sees the exception type."""
        self.dispatcher.submit(MagicMock(side_effect=ValueError("secret database password")), "failing", 7, [])
        self.run_queued()

        self.assertNotIn("secret", str(self.send_response.call_args))
        self.assertIn("secret", str(self.mock_logger.error.call_args))  # Logged locally for debugging


class TestDispatcherThread(DispatcherTest):
    """Tests running the real dispatcher thread."""

    def tearDown(self):
        self.dispatcher.signal_stop()
        self.dispatcher.join(timeout=2)

    def test_handlers_run_on_the_dispatcher_thread(self):
        executions = queue.Queue()

        def handler():
            executions.put((threading.current_thread(), self.dispatcher.on_dispatch_thread()))

        self.dispatcher.start()
        self.dispatcher.submit(handler, "my_method", None, [])

        thread, on_dispatch = executions.get(timeout=2)
        self.assertEqual(thread.name, "Bridge.dispatch_loop")
        self.assertTrue(on_dispatch)
        self.assertFalse(self.dispatcher.on_dispatch_thread())  # False from any other thread

    def test_start_is_idempotent(self):
        self.dispatcher.start()
        thread = self.dispatcher._thread
        self.dispatcher.start()
        self.assertIs(self.dispatcher._thread, thread)

    def test_stop_and_restart_uses_a_fresh_queue(self):
        """Items and stop sentinels from a previous run must not leak into the next one."""
        self.dispatcher.start()
        thread = self.dispatcher._thread
        self.dispatcher.signal_stop()
        self.dispatcher.join(timeout=2)
        self.assertFalse(thread.is_alive())

        stale_queue = self.dispatcher._queue
        stale_queue.put_nowait(None)  # A leftover sentinel that must not stop the next run

        self.dispatcher.start()
        self.assertIsNot(self.dispatcher._queue, stale_queue)

        ran = threading.Event()
        self.dispatcher.submit(lambda: ran.set(), "my_method", None, [])
        self.assertTrue(ran.wait(timeout=2), "Restarted dispatcher did not run the handler")
