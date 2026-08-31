# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import logging
import queue
import select
import socket
import threading
import weakref
from urllib.parse import urlparse

import msgpack

# Library logger: silent unless the application configures the "arduino.router_bridge" namespace
logger = logging.getLogger(__name__)

DEFAULT_ADDRESS = "unix:///var/run/arduino-router.sock"

_reconnect_delay = 3.0  # seconds

# Error codes for RPC messages received from the RPC router. These are defined in the RPC router itself.
ROUTE_ALREADY_EXISTS_ERR = 0x05
BUFFER_LIMIT_EXCEEDED_ERR = 0x06

# Error codes for RPC messages sent to Arduino_RouterBridge. These are defined in the lib itself.
MALFORMED_CALL_ERR = 0xFD
FUNCTION_NOT_FOUND_ERR = 0xFE
GENERIC_ERR = 0xFF


class _BridgeEngine:
    """Internal engine of a `Bridge`: owns the socket, the background read/reconnect
    thread and the handler dispatcher.

    The background threads reference the engine, never the public `Bridge` handle,
    so an unreachable handle can be garbage collected and stop its engine.
    """

    def __init__(
        self, address: str = DEFAULT_ADDRESS, max_message_size: int = 1024 * 1024, max_pending_handlers: int = 1024
    ):
        """Creates a connection for the given router address without connecting.

        Args:
            address (str): The router address, either "unix://<path>" or "tcp://<host>:<port>".
            max_message_size (int): Maximum size in bytes of a single incoming message; the
                connection is dropped and re-established when the peer exceeds it. Defaults to 1 MiB.
            max_pending_handlers (int): Maximum number of queued handler executions; further
                requests are rejected as busy and further notifications dropped. Defaults to 1024.

        Raises:
            ValueError: If the address scheme is not supported or the address is incomplete.
        """
        self.address = address
        urlparsed = urlparse(address)
        if urlparsed.scheme == "unix":
            if not urlparsed.path:
                raise ValueError(f"Invalid unix address '{address}': expected unix://<path>.")
            self.socket_type = "unix"
            self._peer_addr = urlparsed.path
        elif urlparsed.scheme == "tcp":
            try:
                port = urlparsed.port
            except ValueError as e:
                raise ValueError(f"Invalid tcp address '{address}': {e}") from e
            if not urlparsed.hostname or not port:
                raise ValueError(f"Invalid tcp address '{address}': expected tcp://<host>:<port>.")
            self.socket_type = "tcp"
            self._peer_addr = (urlparsed.hostname, port)
        else:
            raise ValueError(
                f"Unsupported scheme '{urlparsed.scheme}' in address '{address}': "
                "expected unix://<path> or tcp://<host>:<port>."
            )

        self._max_message_size = max_message_size
        self._max_pending_handlers = max_pending_handlers

        self.next_msgid = 0  # Guarded by callbacks_lock
        self.callbacks = {}  # msgid -> (on_result, on_error)
        self.callbacks_lock = threading.Lock()
        self.handlers = {}  # method name -> function
        self.handlers_lock = threading.Lock()

        self._conn = None
        self._conn_lock = threading.Lock()  # Guards the _conn reference only
        self._send_lock = threading.Lock()  # Serializes socket writes
        self._lifecycle_lock = threading.Lock()  # Serializes start()/stop()
        self._is_connected_flag = threading.Event()  # This avoids locking recv calls
        self._stop_event = threading.Event()
        self._read_thread = None
        # Incoming requests/notifications for the dispatcher, bounded to cap memory usage
        self._dispatch_queue = queue.Queue(maxsize=max_pending_handlers)
        self._dispatch_thread = None

    def start(self):
        """Starts the background loop that connects to the router and keeps the
        connection alive. Returns immediately: the connection is established in the
        background and retried until it succeeds (see ``wait_connected``).
        A no-op if the background loop is already running.
        """
        with self._lifecycle_lock:
            if self._read_thread is not None and self._read_thread.is_alive():
                return
            self._stop_event.clear()
            # Fresh queue: items and stop sentinels from a previous run must not leak into this one
            self._dispatch_queue = queue.Queue(maxsize=self._max_pending_handlers)
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop, args=(self._dispatch_queue,), name="Bridge.dispatch_loop", daemon=True
            )
            self._dispatch_thread.start()
            self._read_thread = threading.Thread(target=self._conn_manager, name="Bridge.read_loop", daemon=True)
            self._read_thread.start()

    def stop(self):
        """Stops the background loops, closes the connection and releases resources.
        Idempotent and safe to call even if ``start()`` was never called.
        """
        with self._lifecycle_lock:
            self._stop_event.set()
            self._is_connected_flag.clear()

            # Shutdown wakes a blocked recv()/sendall(); _conn_lock is never held during those, so this cannot deadlock
            with self._conn_lock:
                if self._conn is not None:
                    try:
                        self._conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass  # Already disconnected
                    try:
                        self._conn.close()  # Release resources
                    except Exception:
                        pass
                    self._conn = None

            try:
                self._dispatch_queue.put_nowait(None)  # Wake the dispatcher so it can exit
            except queue.Full:
                pass  # The dispatcher is draining items and will notice the stop event by itself

            current = threading.current_thread()
            for thread in (self._read_thread, self._dispatch_thread):
                if thread is not None and thread is not current:
                    thread.join(timeout=_reconnect_delay + 1.0)
                    if thread.is_alive():
                        logger.warning(f"Background thread '{thread.name}' did not terminate in time.")
            self._read_thread = None
            self._dispatch_thread = None

        self._fail_pending_callbacks(ConnectionError("Bridge connection stopped."))

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Waits until the connection to the router is established.

        Args:
            timeout (float, optional): Maximum time to wait in seconds. Waits indefinitely if None.

        Returns:
            bool: True if connected, False if the timeout expired first.
        """
        return self._is_connected_flag.wait(timeout)

    def notify(self, method_name: str, *params):
        """Sends a notification to the server without waiting for a response.
        Best-effort: never blocks waiting for a connection, the notification is
        dropped if the router is not connected.
        """
        request = [2, method_name, params]
        try:
            self._send_bytes(msgpack.packb(request), wait_for_connection=False)
        except ConnectionError:
            logger.debug(f"Dropped notification for method '{method_name}': not connected.")
        except Exception as e:
            logger.error(f"Failed to send notification for method '{method_name}': {e}")

    def call(self, method_name: str, *params, timeout: float | None = 10):
        """Calls a method on the server and waits for a response.
        Waits indefinitely if timeout is None.
        Raises RuntimeError when invoked from a provided handler: the peer may be blocked
        waiting for the handler's own response, so nested calls risk deadlocks and request loops.
        """
        if threading.current_thread() is self._dispatch_thread:
            raise RuntimeError(
                f"Cannot call '{method_name}' from a provided handler: nested bridge calls are not supported. "
                "Use notify for fire-and-forget messages."
            )

        resp_queue = queue.Queue(maxsize=1)

        def on_result(result):
            resp_queue.put((True, result))

        def on_error(error):
            resp_queue.put((False, error))

        # Reserve the message ID and register the callbacks atomically
        with self.callbacks_lock:
            msgid = self._next_msgid_locked()
            self.callbacks[msgid] = (on_result, on_error)

        request = [0, msgid, method_name, params]

        try:
            self._send_bytes(msgpack.packb(request))
        except Exception as e:
            with self.callbacks_lock:
                self.callbacks.pop(msgid, None)
            raise RuntimeError(f"Failed to call method '{method_name}': {e}") from e

        try:
            (success, response) = resp_queue.get(timeout=timeout)
            if success:
                return response
            elif isinstance(response, Exception):
                # The connection dropped or was stopped while the request was pending
                raise ConnectionError(f"Request '{method_name}' failed: {response}") from response
            else:
                err_code, err_msg = response
                raise ValueError(f"Request '{method_name}' failed: {err_msg} ({err_code})")
        except queue.Empty:
            # Timed out waiting for response
            with self.callbacks_lock:
                pending = self.callbacks.pop(msgid, None)
            if pending:
                # Best-effort cancellation outside callbacks_lock: sending blocks and must not stall response dispatching
                try:
                    self.notify("$/cancelRequest", msgid)
                except Exception:
                    pass
            raise TimeoutError(f"Request '{method_name}' timed out after {timeout}s")
        except Exception:
            with self.callbacks_lock:  # Ensure callback is cleaned up on any exception path
                self.callbacks.pop(msgid, None)
            raise

    def provide(self, method_name: str, handler):
        """Makes a method available to the microcontroller, so it can call it remotely.
        The handler should be a callable that can take arguments.

        Registration is declarative: the handler is recorded immediately and registered
        with the router as soon as a connection is available, then re-registered
        transparently on every reconnection. Registration failures are logged.
        """
        if not callable(handler):
            raise ValueError("Handler must be a callable.")

        with self.handlers_lock:
            self.handlers[method_name] = handler

        if self._is_connected_flag.is_set():
            self._register_with_router("$/register", method_name)

    def unprovide(self, method_name: str):
        """Makes a method no more available to the microcontroller."""
        with self.handlers_lock:
            removed = self.handlers.pop(method_name, None)
        if removed is None:
            return  # Nothing to unregister

        if self._is_connected_flag.is_set():
            self._register_with_router("$/unregister", method_name)

    def _register_with_router(self, rpc_method: str, method_name: str):
        """Sends a registration call for the method, logging failures. Runs it in a background
        thread when invoked from a provided handler, since handlers must not block on calls.
        """

        def do_call():
            try:
                self.call(rpc_method, method_name)
            except Exception as e:
                logger.error(f"Failed to send '{rpc_method}' for method '{method_name}': {e}")

        if threading.current_thread() is self._dispatch_thread:
            threading.Thread(target=do_call, name="Bridge.registration", daemon=True).start()
        else:
            do_call()

    def _next_msgid_locked(self):
        """Returns the next message ID not in use by a pending request, within bounds.
        Must be called while holding callbacks_lock.
        """
        self.next_msgid = (self.next_msgid + 1) % (2**32)
        while self.next_msgid in self.callbacks:
            self.next_msgid = (self.next_msgid + 1) % (2**32)
        return self.next_msgid

    def _dispatch_loop(self, dispatch_queue):
        """Runs incoming request/notification handlers off the read thread, so slow
        handlers cannot stall message processing.
        Exits when the stop sentinel (None) is received.
        """
        while True:
            item = dispatch_queue.get()
            if item is None or self._stop_event.is_set():
                return
            self._run_handler(*item)

    def _run_handler(self, handler, method_name, msgid, params):
        """Executes a user-provided handler, replying to the router when msgid identifies a request.
        Handler exceptions are reported to the peer by exception type only; full details stay in the local log.
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

    def _conn_manager(self):
        """Manages connection and reconnection attempts. Once the connection is established, delegates to the read loop."""
        while not self._stop_event.is_set():
            # Ensure we're connected to the router
            self._connect()  # This retries internally until connected or stop() is requested
            if self._stop_event.is_set():
                break
            self._read_loop()  # This blocks until connection is lost or errors out
            if self._stop_event.is_set():
                break
            self._stop_event.wait(_reconnect_delay)

    def _connect(self):
        """Makes sure we're connected to the router by retrying periodically until we have a clean connection.
        This method **must be** the only one allowed to set _is_connected_flag, this allows us to use a
        lockless algorithm for connection management, in particular for recv calls.
        """
        if self._is_connected():
            return

        self._is_connected_flag.clear()

        if self._conn:
            with self._conn_lock:
                # Dirty state: we have a _conn object but we're not connected, drop the broken connection object
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

        while not self._is_connected():
            if self._stop_event.is_set():
                return
            try:
                if self.socket_type == "unix":
                    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    conn.connect(self._peer_addr)
                else:
                    host, port = self._peer_addr
                    conn = socket.create_connection((host, port), timeout=5)
                conn.settimeout(None)  # Set blocking recv
                with self._conn_lock:
                    self._conn = conn
                self._is_connected_flag.set()

                if self._stop_event.is_set():
                    # stop() may have run before the connection was published: undo and bail out
                    self._is_connected_flag.clear()
                    with self._conn_lock:
                        self._conn = None
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return

                # Register in a separate thread: each call blocks for a response that arrives once the read loop runs
                def register_methods_on_connect():
                    with self.handlers_lock:
                        methods = list(self.handlers.keys())
                    for method in methods:  # Register outside handlers_lock: each call blocks for a response
                        try:
                            self.call("$/register", method)
                        except Exception as e:
                            logger.error(f"Failed to register method '{method}' after connection: {e}")

                if self.handlers:
                    t = threading.Thread(
                        target=register_methods_on_connect, name="Bridge.register_methods_on_connect", daemon=True
                    )
                    t.start()

                return
            except Exception as e:
                logger.error(f"Failed to connect to router: {e}")
                self._stop_event.wait(_reconnect_delay)

    def _is_connected(self) -> bool:
        """Performs a lightweight check to verify if the connection is usable and active.
        Takes care to not block or remove bytes from the buffer.
        """
        conn = self._conn
        if conn is None:
            return False

        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if not readable:
                return True  # Socket is open and reading from it would block
            # Peek without consuming buffered bytes; select guarantees this recv won't block
            data = conn.recv(8, socket.MSG_PEEK)
            if len(data) == 0:
                return False
            return True
        except ConnectionResetError as e:
            logger.warning(f"Connection reset in connection loop: {e}")
            return False  # Socket was closed for some other reason
        except Exception as e:
            logger.error(f"Unexpected error while checking socket status: {e}")
            return False  # Assume the socket is broken for any other exception

    def _read_loop(self):
        """The core loop that reads and processes messages from the active socket.
        Returns when the connection is lost or stop is requested. Reconnection is
        handled by the caller.
        """
        conn = self._conn
        unpacker = msgpack.Unpacker(max_buffer_size=self._max_message_size)
        try:
            while not self._stop_event.is_set():
                try:
                    data = conn.recv(4096)
                    if not data:
                        logger.info("Connection closed by router")
                        break
                    unpacker.feed(data)
                    for msg in unpacker:
                        self._handle_msg(msg)
                except msgpack.exceptions.BufferFull:
                    logger.error(f"Incoming message exceeds the {self._max_message_size} bytes limit, reconnecting")
                    break
                except ConnectionResetError as e:
                    logger.warning(f"Connection reset in read loop: {e}")
                    break
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.error(f"Unexpected error in read loop: {e}")
                    break
        finally:
            # Connection was lost unexpectedly but we were meant to be running, tell the user
            self._fail_pending_callbacks(ConnectionError("Connection to router lost."))

    # The arduino-router guarantees str-encoded method names; anything else is a malformed message
    def _decode_method(self, method_name) -> str:
        """Validates that the method name arrived as a string."""
        if not isinstance(method_name, str):
            raise ValueError(f"Invalid method name type: {type(method_name)}. Expected str.")
        return method_name

    def _handle_msg(self, msg: list):
        """Processes a single deserialized MessagePack-RPC message."""
        if not msg or not isinstance(msg, list):
            logger.warning("Invalid RPC message received (must be a non-empty list).")
            return

        msg_type = msg[0]
        try:
            if msg_type == 0:  # Request: [0, msgid, method, params]
                if len(msg) != 4:
                    raise ValueError(f"Invalid RPC request: expected length 4, got {len(msg)}")
                _, msgid, method, params = msg
                if not isinstance(params, (list, tuple)):
                    raise ValueError("Invalid RPC request params: expected array or tuple")

                method_name = self._decode_method(method)

                with self.handlers_lock:
                    handler = self.handlers.get(method_name)

                if handler:
                    try:
                        # Hand off to the dispatcher thread: user code must not run on the read thread
                        self._dispatch_queue.put_nowait((handler, method_name, msgid, params))
                    except queue.Full:
                        logger.warning(f"Handler queue full, rejecting request for method '{method_name}'")
                        self._send_response(msgid, [GENERIC_ERR, "Server busy: too many pending requests."], None)
                else:
                    self._send_response(msgid, [FUNCTION_NOT_FOUND_ERR, f"Method not found: '{method_name}'"], None)

            elif msg_type == 1:  # Response: [1, msgid, error, result]
                if len(msg) != 4:
                    raise ValueError(f"Invalid RPC response: expected length 4, got {len(msg)}")
                _, msgid, error, result = msg
                if error and (not isinstance(error, list) or len(error) < 2):
                    raise ValueError("Invalid error format in RPC response")

                with self.callbacks_lock:
                    cbs = self.callbacks.pop(msgid, None)
                if cbs:
                    on_result, on_error = cbs
                    if result is None and error is None:
                        on_result(None)
                    else:
                        # Treat ROUTE_ALREADY_EXISTS_ERR as OK: the router already knows the method, a recoverable situation
                        if result is not None or (error is not None and error[0] == ROUTE_ALREADY_EXISTS_ERR):
                            on_result(result)
                        elif error is not None:
                            on_error(error)
                        else:
                            on_result([GENERIC_ERR, "Unknown error occurred."])
                else:
                    logger.warning(f"Response for unknown msgid {msgid} received.")

            elif msg_type == 2:  # Notification: [2, method, params]
                if len(msg) != 3:
                    raise ValueError(f"Invalid RPC notification: expected length 3, got {len(msg)}")
                _, method, params = msg
                if not isinstance(params, (list, tuple)):
                    raise ValueError("Invalid RPC notification params: expected array or tuple")

                method_name = self._decode_method(method)

                with self.handlers_lock:
                    handler = self.handlers.get(method_name)

                if handler:
                    try:
                        # Hand off to the dispatcher thread; msgid None marks a notification (no response)
                        self._dispatch_queue.put_nowait((handler, method_name, None, params))
                    except queue.Full:
                        logger.warning(f"Handler queue full, dropping notification for method '{method_name}'")
            else:
                logger.warning(f"Invalid RPC message type received: {msg_type}")

        except ValueError as ve:
            logger.error(f"Message validation error: {ve}")
        except Exception as e:
            logger.error(f"Unexpected error while handling message: {e}")

    def _fail_pending_callbacks(self, reason: Exception):
        """Invokes error callbacks for all pending requests and clears their callbacks."""
        with self.callbacks_lock:
            for _, (_, on_error) in list(self.callbacks.items()):
                if on_error:
                    try:
                        on_error(reason)
                    except Exception as e:
                        logger.error(f"Failed to run 'on_error' callback: {e}")
            self.callbacks.clear()

    def _send_response(self, msgid: int, err: list | None, response):
        """Helper to pack and send a response message. err is None or an [err_code, err_msg] pair."""
        msg = [1, msgid, err, response]
        try:
            # Don't wait for a reconnection: the requester's msgid belongs to the connection that carried it
            self._send_bytes(msgpack.packb(msg), wait_for_connection=False)
        except ConnectionError:
            pass  # Response sending is best-effort if connection drops while handling request.
        except Exception as e:  # e.g., msgpack encoding error
            logger.error(f"Failed to pack/send response: {e}")

    def _send_bytes(self, packed_data: bytes, wait_for_connection: bool = True):
        """Sends packed data, handling connection waits and errors.
        With wait_for_connection, a disconnected bridge is given a grace period to
        reconnect before failing; otherwise it fails immediately.
        """
        if not self._is_connected_flag.is_set():
            # Wait hoping for an auto-reconnection by _conn_manager
            if not wait_for_connection or not self._is_connected_flag.wait(timeout=_reconnect_delay):
                raise ConnectionError("Not connected to router, send failed.")

        with self._conn_lock:
            conn = self._conn
        if conn is None:
            raise ConnectionError("No connection object for router, send failed.")

        # The dedicated send lock lets stop() shut the socket down even while a send is blocked mid-transfer
        with self._send_lock:
            try:
                conn.sendall(packed_data)
            except socket.error as e:
                raise ConnectionError(f"Send failed due to socket error: {e}")


class Bridge:
    """A MessagePack-RPC bridge to an Arduino router.

    Instances are independent: create one per router you need to talk to, call
    ``connect()`` to establish the link in the background, and ``disconnect()``
    when done. It can also be used as a context manager, and an instance that
    becomes garbage collected disconnects automatically as a safety net.

    How an instance is shared is the caller's concern: an embedding runtime that
    needs a process-wide bridge creates one instance and exposes it itself.

    Provided handlers run sequentially, in arrival order, on a dedicated dispatcher
    thread; a slow handler delays the handlers queued after it. A handler may send
    notifications, but must not call back into the bridge with ``call``: the peer may
    be blocked waiting for the handler's own response, so nested calls risk deadlocks
    and request loops and are rejected with a RuntimeError.

    Examples:
        bridge = Bridge()
        bridge.connect()
        temperature = bridge.call("get_temperature", "sensor1")
        bridge.provide("get_status", lambda: "ok")
        bridge.notify("set_led", "green", True)
        bridge.disconnect()
    """

    def __init__(
        self, address: str = DEFAULT_ADDRESS, max_message_size: int = 1024 * 1024, max_pending_handlers: int = 1024
    ):
        """Creates a bridge for the given router address without connecting.

        Args:
            address (str): The router address, either "unix://<path>" or "tcp://<host>:<port>".
            max_message_size (int): Maximum size in bytes of a single incoming message; the
                connection is dropped and re-established when the peer exceeds it. Defaults to 1 MiB.
            max_pending_handlers (int): Maximum number of queued handler executions; further
                requests are rejected as busy and further notifications dropped. Defaults to 1024.

        Raises:
            ValueError: If the address scheme is not supported or the address is incomplete.
        """
        self._engine = _BridgeEngine(address, max_message_size, max_pending_handlers)
        # The engine never references the handle: collecting an abandoned handle stops its engine
        self._finalizer = weakref.finalize(self, self._engine.stop)

    @property
    def address(self) -> str:
        """The router address this bridge points to."""
        return self._engine.address

    def connect(self):
        """Starts connecting to the router in the background and returns immediately.
        The connection is retried until it succeeds (see ``wait_connected``) and
        re-established automatically whenever it is lost. A no-op if already running.
        """
        self._engine.start()

    def disconnect(self):
        """Closes the connection and releases resources. Idempotent and safe to call
        even if ``connect()`` was never called; ``connect()`` can be called again afterwards.
        """
        self._engine.stop()

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Waits until the connection to the router is established.

        Args:
            timeout (float, optional): Maximum time to wait in seconds. Waits indefinitely if None.

        Returns:
            bool: True if connected, False if the timeout expired first.
        """
        return self._engine.wait_connected(timeout)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()

    def notify(self, method_name: str, *params):
        """Sends a notification to the microcontroller without waiting for a response.
        Best-effort: never blocks waiting for a connection, the notification is
        dropped if the router is not connected.

        Args:
            method_name (str): The name of the method to notify on the microcontroller.
            *params: The parameters to pass to the method.

        Examples:
            bridge.notify("set_led", "green", True)
        """
        self._engine.notify(method_name, *params)

    def call(self, method_name: str, *params, timeout: float | None = 10):
        """Calls a method on the microcontroller and waits for a response.
        Raises an exception if the call fails or times out.

        Args:
            method_name (str): The name of the method to call on the microcontroller.
            *params: The parameters to pass to the method.
            timeout (float, optional): The maximum time to wait for a response in seconds.
                If None, waits indefinitely. Defaults to 10s.

        Raises:
            ValueError: If the method does not exist or the call fails.
            TimeoutError: If the call takes more time than the specified timeout.
            ConnectionError: If the connection drops or is stopped while waiting.
            RuntimeError: If invoked from a provided handler (nested calls are not
                supported), or if the call fails unexpectedly.

        Examples:
            temperature = bridge.call("get_temperature", "sensor1")
        """
        return self._engine.call(method_name, *params, timeout=timeout)

    def provide(self, method_name: str, handler):
        """Makes a method available to the microcontroller, so it can call it remotely.
        The handler should be a callable that can take arguments.

        Registration is declarative: the handler is recorded immediately, registered
        with the router as soon as a connection is available, and re-registered
        transparently on every reconnection.

        The handler may send notifications but must not call back into the bridge
        with ``call``: nested calls are rejected with a RuntimeError (see ``call``).

        Args:
            method_name (str): The name under which the function should be provided to the microcontroller.
            handler (callable): The function to call when the microcontroller requires it.

        Raises:
            ValueError: If handler is not callable.

        Examples:
            bridge.provide("get_country", get_country)
        """
        self._engine.provide(method_name, handler)

    def unprovide(self, method_name: str):
        """Makes a method no more available to the microcontroller.

        Args:
            method_name (str): The name under which the function is already provided to the microcontroller.

        Examples:
            bridge.unprovide("get_country")
        """
        self._engine.unprovide(method_name)
