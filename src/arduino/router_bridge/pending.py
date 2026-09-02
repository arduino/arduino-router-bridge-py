# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

"""Registry of in-flight requests awaiting a response from the router."""

import logging
import threading

logger = logging.getLogger(__name__)


class PendingCalls:
    """Allocates message IDs and holds the callbacks of in-flight requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._callbacks = {}  # msgid -> (on_result, on_error)
        self._next_msgid = 0

    def register(self, on_result, on_error) -> int:
        """Reserves the next free message ID and records its callbacks atomically."""
        with self._lock:
            msgid = (self._next_msgid + 1) % (2**32)
            while msgid in self._callbacks:
                msgid = (msgid + 1) % (2**32)
            self._next_msgid = msgid
            self._callbacks[msgid] = (on_result, on_error)
            return msgid

    def pop(self, msgid: int) -> tuple | None:
        """Consumes and returns the (on_result, on_error) pair for msgid, or None."""
        with self._lock:
            return self._callbacks.pop(msgid, None)

    def fail_all(self, reason: Exception):
        """Invokes the error callback of every pending request with reason and clears the registry."""
        with self._lock:
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        # Callbacks run outside the lock.
        for _, on_error in callbacks:
            if on_error:
                try:
                    on_error(reason)
                except Exception as e:
                    logger.error(f"Failed to run 'on_error' callback: {e}")
