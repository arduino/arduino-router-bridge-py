# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

"""Registry of provided handlers and the dedicated thread that runs them."""

import logging
import queue
import threading

from .protocol import GENERIC_ERR, MALFORMED_CALL_ERR

logger = logging.getLogger(__name__)


class Dispatcher:
    """Holds the handlers provided to the peer and runs them sequentially, in arrival order,
    on a dedicated thread, so slow handlers cannot stall the read loop that feeds them.
    The queue is bounded: ``submit`` reports a full queue instead of growing without limit.
    """

    def __init__(self, max_pending: int, send_response):
        """send_response(msgid, err, result) is called to answer the request a handler ran for."""
        self._max_pending = max_pending
        self._send_response = send_response
        self._handlers = {}  # method name -> function
        self._handlers_lock = threading.Lock()
        self._queue = queue.Queue(maxsize=max_pending)
        self._stop_event = threading.Event()
        self._thread = None

    def add(self, method_name: str, handler):
        if not callable(handler):
            raise ValueError("Handler must be a callable.")
        with self._handlers_lock:
            self._handlers[method_name] = handler

    def remove(self, method_name: str):
        """Removes and returns the handler, or None if not registered."""
        with self._handlers_lock:
            return self._handlers.pop(method_name, None)

    def lookup(self, method_name: str):
        with self._handlers_lock:
            return self._handlers.get(method_name)

    def method_names(self) -> list:
        with self._handlers_lock:
            return list(self._handlers.keys())

    def start(self):
        """Starts the dispatcher thread; a no-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        # Fresh queue: items and stop sentinels from a previous run must not leak into this one
        self._queue = queue.Queue(maxsize=self._max_pending)
        self._thread = threading.Thread(
            target=self._loop, args=(self._queue,), name="Bridge.dispatch_loop", daemon=True
        )
        self._thread.start()

    def signal_stop(self):
        """Signals the dispatcher thread to exit, without waiting for it."""
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)  # Wake the dispatcher so it can exit
        except queue.Full:
            pass  # The dispatcher is draining items and will notice the stop event by itself

    def join(self, timeout: float):
        """Waits for the dispatcher thread to terminate, logging if it does not in time.
        A no-op from the dispatcher thread itself: a handler calling stop cannot self-join.
        """
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning(f"Background thread '{thread.name}' did not terminate in time.")
        self._thread = None

    def on_dispatch_thread(self) -> bool:
        """True when the calling code runs on the dispatcher thread, i.e. inside a handler."""
        return threading.current_thread() is self._thread

    def submit(self, handler, method_name: str, msgid, params) -> bool:
        """Queues a handler execution, False when the queue is full; msgid None marks a notification."""
        try:
            self._queue.put_nowait((handler, method_name, msgid, params))
            return True
        except queue.Full:
            return False

    def _loop(self, dispatch_queue):
        while True:
            item = dispatch_queue.get()
            if item is None or self._stop_event.is_set():
                return
            self._run_handler(*item)

    def _run_handler(self, handler, method_name, msgid, params):
        """Executes a user-provided handler, replying to the router when msgid identifies a request.
        Only the exception type reaches the peer; full details stay in the local log.
        """
        try:
            result = handler(*params)
            if msgid is not None:
                self._send_response(msgid, None, result)
        except Exception as e:
            logger.error(f"Failed to run user-provided handler for method '{method_name}': {e}", exc_info=True)
            if msgid is not None:
                err_code = MALFORMED_CALL_ERR if isinstance(e, (TypeError, ValueError)) else GENERIC_ERR
                self._send_response(msgid, [err_code, f"Unhandled {type(e).__name__} in handler"], None)
