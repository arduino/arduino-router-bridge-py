# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import logging
import queue
import socket
import threading
from urllib.parse import urlparse

import msgpack

# Standard library logger: silent unless the application attaches a handler to the
# "arduino.router_bridge" namespace or enables propagation to the root logger
logger = logging.getLogger(__name__)

DEFAULT_ADDRESS = "unix:///var/run/arduino-router.sock"

_reconnect_delay = 3.0  # seconds

# Error codes for RPC messages received from the RPC router. These are defined in the RPC router itself.
ROUTE_ALREADY_EXISTS_ERR = 0x05
BUFFER_LIMIT_EXCEEDED_ERR = 0x06

# Error codes for RPC messages sent to Arduino_RPClite. These are defined in the lib itself.
MALFORMED_CALL_ERR = 0xFD
FUNCTION_NOT_FOUND_ERR = 0xFE
GENERIC_ERR = 0xFF


class BridgeConnection:
    """A single connection to an RPC router. Owns the socket and the background
    read/reconnect thread.

    Instances are independent: create one per router you need to talk to. For the
    common single-router case, prefer the process-wide shared instances managed by
    the `Bridge` API and the decorators (see the `bridge` module).

    Requires a call to ``start()`` to connect and ``stop()`` to release resources,
    both methods are idempotent. It can also be used as a context manager.
    """

    def __init__(self, address: str = DEFAULT_ADDRESS):
        """Creates a connection for the given router address without connecting.

        Args:
            address (str): The router address, either "unix://<path>" or "tcp://<host>:<port>".

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

        self.next_msgid = 0
        self.next_msgid_lock = threading.Lock()
        self.callbacks = {}  # msgid -> (on_result, on_error)
        self.callbacks_lock = threading.Lock()
        self.handlers = {}  # method name -> function
        self.handlers_lock = threading.Lock()

        self._conn = None
        self._conn_lock = threading.Lock()
        self._is_connected_flag = threading.Event()  # This avoids locking recv calls
        self._stop_event = threading.Event()
        self._read_thread = None

    def start(self):
        """Starts the background loop that connects to the router and keeps the
        connection alive. Returns immediately: the connection is established in the
        background and retried until it succeeds (see ``wait_connected``).
        A no-op if the background loop is already running.
        """
        if self._read_thread is not None and self._read_thread.is_alive():
            return
        self._stop_event.clear()
        self._read_thread = threading.Thread(target=self._conn_manager, name="Bridge.read_loop", daemon=True)
        self._read_thread.start()

    def stop(self):
        """Stops the background loop, closes the connection and releases resources.
        Idempotent and safe to call even if ``start()`` was never called.
        """
        self._stop_event.set()
        self._is_connected_flag.clear()

        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.shutdown(socket.SHUT_RDWR)  # Wake a recv() on the read side
                except OSError:
                    pass  # Already disconnected
                try:
                    self._conn.close()  # Release resources
                except Exception:
                    pass
                self._conn = None

        thread = self._read_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_reconnect_delay + 1.0)
        self._read_thread = None

        self._fail_pending_callbacks(ConnectionError("Bridge connection stopped."))

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Waits until the connection to the router is established.

        Args:
            timeout (float, optional): Maximum time to wait in seconds. Waits indefinitely if None.

        Returns:
            bool: True if connected, False if the timeout expired first.
        """
        return self._is_connected_flag.wait(timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def notify(self, method_name: str, *params):
        """Sends a notification to the server without waiting for a response."""
        request = [2, method_name, params]
        try:
            self._send_bytes(msgpack.packb(request))
        except ConnectionError:
            # Fire-and-forget semantics
            pass
        except Exception as e:
            logger.error(f"Failed to send notification for method '{method_name}': {e}")

    def call(self, method_name: str, *params, timeout: float | None = 10):
        """Calls a method on the server and waits for a response.
        Waits indefinitely if timeout is None.
        """
        msgid = self._increment_next_msgid()
        request = [0, msgid, method_name, params]

        resp_queue = queue.Queue(maxsize=1)

        def on_result(result):
            resp_queue.put((True, result))

        def on_error(error):
            resp_queue.put((False, error))

        with self.callbacks_lock:
            self.callbacks[msgid] = (on_result, on_error)

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
            else:
                err_code, err_msg = response
                raise ValueError(f"Request '{method_name}' failed: {err_msg} ({err_code})")
        except queue.Empty:
            # Timed out waiting for response
            with self.callbacks_lock:
                if self.callbacks.pop(msgid, None):
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
            try:
                self.call("$/register", method_name)
            except Exception as e:
                logger.error(f"Failed to register method '{method_name}' with the router: {e}")

    def unprovide(self, method_name: str):
        """Makes a method no more available to the microcontroller."""
        with self.handlers_lock:
            removed = self.handlers.pop(method_name, None)
        if removed is None:
            return  # Nothing to unregister

        if self._is_connected_flag.is_set():
            try:
                self.call("$/unregister", method_name)
            except Exception as e:
                logger.error(f"Failed to unregister method '{method_name}' from the router: {e}")

    def _increment_next_msgid(self):
        """Increments the next message ID, ensuring it is unique and within bounds."""
        with self.next_msgid_lock:
            self.next_msgid = (self.next_msgid + 1) % (2**32)
            while self.next_msgid in self.callbacks:
                self.next_msgid = (self.next_msgid + 1) % (2**32)
            return self.next_msgid

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

        if self._conn:
            with self._conn_lock:
                # We're in a dirty state since we have a valid _conn object but looks like we're not connected.
                # Clean up the old, probably broken, connection object.
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

        self._is_connected_flag.clear()

        while not self._is_connected():
            if self._stop_event.is_set():
                return
            try:
                with self._conn_lock:
                    if self.socket_type == "unix":
                        self._conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        self._conn.connect(self._peer_addr)
                    elif self.socket_type == "tcp":
                        self._conn = socket.create_connection(self._peer_addr, timeout=5)
                self._conn.settimeout(None)  # Set blocking recv
                self._is_connected_flag.set()

                # Run this function in a separate thread for receiving the call response as it would block waiting for the response
                def register_methods_on_reconnect():
                    with self.handlers_lock:
                        for method in self.handlers.keys():
                            try:
                                self.call("$/register", method)
                            except Exception as e:
                                logger.error(f"Failed to re-register method '{method}' after reconnection: {e}")

                if self.handlers:
                    t = threading.Thread(
                        target=register_methods_on_reconnect, name="Bridge.register_methods_on_reconnect", daemon=True
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
        if self._conn is None:
            return False

        try:
            # Make sure we don't block or remove bytes from the buffer (peek only)
            data = self._conn.recv(8, socket.MSG_DONTWAIT | socket.MSG_PEEK)
            if len(data) == 0:
                return False
            return True
        except BlockingIOError:
            return True  # Socket is open and reading from it would block
        except ConnectionResetError as e:
            logger.warning(f"Connection reset in connection loop: {e}")
            return False  # Socket was closed for some other reason
        except Exception as e:
            logger.error(f"Unexpected error while checking socket status: {e}")
            return False  # Assume the socket is broken for any other exception

    def _read_loop(self):
        """The core loop that reads and processes messages from the active socket."""
        unpacker = msgpack.Unpacker()
        try:
            while not self._stop_event.is_set():
                try:
                    data = self._conn.recv(4096)
                    if not data:
                        logger.info("Connection closed by router")
                        break
                    unpacker.feed(data)
                    for msg in unpacker:
                        self._handle_msg(msg)
                except ConnectionResetError as e:
                    logger.warning(f"Connection reset in read loop: {e}")
                    break
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.error(f"Unexpected error in read loop: {e}")
                    continue
        finally:
            # Connection was lost unexpectedly but we were meant to be running, tell the user
            self._fail_pending_callbacks(ConnectionError("Connection to router lost."))

    # TODO: verify if this is still needed
    def _decode_method(self, method_name: any) -> str:
        """Decodes the method name from bytes to string if necessary."""
        if isinstance(method_name, bytes):
            return method_name.decode()
        if isinstance(method_name, str):
            return method_name
        else:
            raise ValueError(f"Invalid method name type: {type(method_name)}. Expected str or bytes.")

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
                        result = handler(*params)  # Unpack params
                        self._send_response(msgid, None, result)
                    except Exception as e:
                        logger.error(f"Failed to run user-provided call handler for method '{method_name}': {e}")
                        self._send_response(msgid, e, None)
                else:
                    self._send_response(msgid, NameError(f"Method not found: '{method_name}'", method_name), None)

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
                        # Treat ROUTE_ALREADY_EXISTS_ERR error as OK. It only means that the router already knows about the
                        # method and registering it is not necessary. It's an internal and recoverable situation.
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
                        handler(*params)
                    except Exception as e:
                        logger.error(
                            f"Failed to run user-provided notification handler for method '{method_name}': {e}"
                        )
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

    def _send_response(self, msgid: int, error, response):
        """Helper to pack and send a response message."""
        err = None
        if error is not None:
            err_code = GENERIC_ERR
            err_msg = str(error)
            if isinstance(error, NameError):
                err_code = FUNCTION_NOT_FOUND_ERR
            elif isinstance(error, TypeError) or isinstance(error, ValueError):
                err_code = MALFORMED_CALL_ERR
            err = [err_code, err_msg]

        msg = [1, msgid, err, response]
        try:
            self._send_bytes(msgpack.packb(msg))
        except ConnectionError:
            pass  # Response sending is best-effort if connection drops while handling request.
        except Exception as e:  # e.g., msgpack encoding error
            logger.error(f"Failed to pack/send response: {e}")

    def _send_bytes(self, packed_data: bytes):
        """Sends packed data, handling connection waits and errors."""
        if not self._is_connected_flag.is_set():
            # Wait hoping for an auto-reconnection by _conn_manager
            if not self._is_connected_flag.wait(timeout=_reconnect_delay):
                raise ConnectionError("Not connected to router, send failed.")

        with self._conn_lock:
            if self._conn is None:
                raise ConnectionError("No connection object for router, send failed.")
            try:
                self._conn.sendall(packed_data)
            except socket.error as e:
                raise ConnectionError(f"Send failed due to socket error: {e}")
