# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge.pending import PendingCalls


class TestPendingCalls(unittest.TestCase):
    def setUp(self):
        self.mock_logger = MagicMock()
        logger_patcher = patch("arduino.router_bridge.pending.logger", self.mock_logger)
        logger_patcher.start()
        self.addCleanup(logger_patcher.stop)
        self.pending = PendingCalls()

    def test_register_allocates_sequential_ids(self):
        self.assertEqual(self.pending.register(None, None), 1)
        self.assertEqual(self.pending.register(None, None), 2)

    def test_register_skips_pending_ids(self):
        """Message IDs of pending requests must never be reused."""
        first = self.pending.register(None, None)
        second = self.pending.register(None, None)
        self.pending._next_msgid = first - 1  # Force the allocator to walk over the pending IDs
        self.assertEqual(self.pending.register(None, None), second + 1)

    def test_register_wraps_around(self):
        """The msgid space is 32-bit: allocation wraps instead of growing unbounded."""
        self.pending._next_msgid = 2**32 - 1
        self.assertEqual(self.pending.register(None, None), 0)

    def test_pop_consumes_the_entry(self):
        on_result, on_error = MagicMock(), MagicMock()
        msgid = self.pending.register(on_result, on_error)

        self.assertEqual(self.pending.pop(msgid), (on_result, on_error))
        self.assertIsNone(self.pending.pop(msgid))  # Consumed: at most one resolution per request

    def test_pop_unknown_id(self):
        self.assertIsNone(self.pending.pop(9999))

    def test_fail_all_invokes_error_callbacks_and_clears(self):
        on_error_1, on_error_2 = MagicMock(), MagicMock()
        msgid_1 = self.pending.register(None, on_error_1)
        msgid_2 = self.pending.register(None, on_error_2)

        reason = ConnectionError("Connection to router lost.")
        self.pending.fail_all(reason)

        on_error_1.assert_called_once_with(reason)
        on_error_2.assert_called_once_with(reason)
        self.assertIsNone(self.pending.pop(msgid_1))
        self.assertIsNone(self.pending.pop(msgid_2))

    def test_fail_all_tolerates_callback_errors(self):
        """A failing callback is logged and must not prevent failing the remaining ones."""
        failing = MagicMock(side_effect=RuntimeError("boom"))
        on_error = MagicMock()
        self.pending.register(None, failing)
        self.pending.register(None, on_error)

        self.pending.fail_all(ConnectionError("lost"))

        on_error.assert_called_once()
        self.mock_logger.error.assert_called_once()

    def test_fail_all_runs_callbacks_outside_the_lock(self):
        """Callbacks are caller-provided and may call back into the registry: no deadlock allowed."""
        reentered = []

        def on_error(reason):
            reentered.append(self.pending.register(None, None))  # Would deadlock if the lock were held

        self.pending.register(None, on_error)
        self.pending.fail_all(ConnectionError("lost"))
        self.assertEqual(len(reentered), 1)
