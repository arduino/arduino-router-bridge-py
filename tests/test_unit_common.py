# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

from arduino.router_bridge.connection import _BridgeConnection
from arduino.router_bridge.transport import DEFAULT_ADDRESS


class UnitTest(unittest.TestCase):
    """Base for unit tests: silences logging and builds connections without background threads."""

    def setUp(self):
        """Patches every module logger with one shared mock so tests assert on log output in one place."""
        self.mock_logger = MagicMock()
        for module in ("connection", "dispatch", "pending", "transport"):
            patcher = patch(f"arduino.router_bridge.{module}.logger", self.mock_logger)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_connection(self, address=DEFAULT_ADDRESS, **kwargs):
        """Creates a connection without starting its background threads: tests drive it directly."""
        return _BridgeConnection(address, **kwargs)

    def connect_transport(self, client):
        """Attaches a mock transport and marks the connection as established, standing in for _connect."""
        transport = MagicMock()
        client._transport = transport
        client._is_connected_flag.set()
        return transport

    def mark_dispatch_thread(self, client):
        """Marks the current thread as the dispatcher thread, standing in for handler context."""
        client._dispatcher._thread = threading.current_thread()

    def drain_dispatch(self, client):
        """Runs queued handler dispatches synchronously, standing in for the dispatcher thread."""
        dispatcher = client._dispatcher
        while True:
            try:
                item = dispatcher._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                dispatcher._run_handler(*item)

    def synchronous_threads(self):
        """Patches connection-spawned threads to run their target inline, for deterministic tests."""

        def run_inline(target=None, *args, **kwargs):
            target()
            return MagicMock()

        return patch("arduino.router_bridge.connection.threading.Thread", side_effect=run_inline)
