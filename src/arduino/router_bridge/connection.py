# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

"""Connection lifecycle behind the public Bridge facade: composes a Transport, a
PendingCalls registry and a Dispatcher into the reconnect/read loops and message routing."""

import logging
import queue
import threading

import msgpack

from . import protocol
from .dispatch import Dispatcher
from .pending import PendingCalls
from .transport import DEFAULT_ADDRESS, Transport, parse_address

logger = logging.getLogger(__name__)

_reconnect_delay = 3.0  # seconds

__all__ = ["DEFAULT_ADDRESS", "_BridgeConnection"]


class _BridgeConnection:
    """Internal connection manager: owns the transport, the background read/reconnect thread and the dispatching.

    The background threads never reference this connection, so an unreachable handle can be garbage collected.
    """

    def __init__(
        self, address: str = DEFAULT_ADDRESS, max_message_size: int = 1024 * 1024, max_pending_handlers: int = 1024
    ):
        """Creates a connection without connecting; see ``Bridge`` for the parameters."""
        self.address = address
        self._socket_type, self._peer_addr = parse_address(address)
        self._max_message_size = max_message_size

        self._pending = PendingCalls()
        self._dispatcher = Dispatcher(max_pending_handlers, self._send_response)

        self._transport = None
        self._transport_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()  # Serializes start()/stop()
        self._is_connected_flag = threading.Event()  # This avoids locking recv calls
        self._stop_event = threading.Event()
        self._read_thread = None

    def start(self):
        """Starts the background loops; returns immediately, connecting and retrying in the background."""
        with self._lifecycle_lock:
            if self._read_thread is not None and self._read_thread.is_alive():
                return
            self._stop_event.clear()
            self._dispatcher.start()
            self._read_thread = threading.Thread(target=self._conn_manager, name="Bridge.read_loop", daemon=True)
            self._read_thread.start()

    def stop(self):
        """Stops the background loops and closes the connection. Idempotent; blocks briefly while
        the threads wind down, so contexts that must not block use ``_signal_stop`` instead.
        """
        with self._lifecycle_lock:
            self._signal_stop()

            if self._read_thread is not None and self._read_thread is not threading.current_thread():
                self._read_thread.join(timeout=_reconnect_delay + 1.0)
                if self._read_thread.is_alive():
                    logger.warning(f"Background thread '{self._read_thread.name}' did not terminate in time.")
            self._read_thread = None
            self._dispatcher.join(timeout=_reconnect_delay + 1.0)

        self._pending.fail_all(ConnectionError("Bridge connection stopped."))

    def _signal_stop(self):
        """Signals the loops to exit and tears down the transport. Never blocks, so it is safe
        from a weakref finalizer: the daemon threads notice the stop event on their own.
        """
        self._stop_event.set()
        self._is_connected_flag.clear()

        # close() wakes a blocked recv()/send(); _transport_lock is never held during those, so this cannot deadlock
        with self._transport_lock:
            transport = self._transport
            self._transport = None
        if transport is not None:
            transport.close()

        self._dispatcher.signal_stop()

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Waits until connected: True if connected, False if the timeout expired first."""
        return self._is_connected_flag.wait(timeout)

    def notify(self, method_name: str, *params):
        """Sends a notification, best-effort: never waits for a connection, dropped if disconnected."""
        try:
            self._send_bytes(protocol.pack_notification(method_name, params), wait_for_connection=False)
        except ConnectionError:
            logger.debug(f"Dropped notification for method '{method_name}': not connected.")
        except Exception as e:
            logger.error(f"Failed to send notification for method '{method_name}': {e}")

    def call(self, method_name: str, *params, timeout: float | None = 10):
        """Calls a method and waits for its response (indefinitely if timeout is None).
        Rejected on the dispatcher thread: the peer may be blocked waiting for the handler's
        own response, so nested calls risk deadlocks and request loops.
        """
        if self._dispatcher.on_dispatch_thread():
            raise RuntimeError(
                f"Cannot call '{method_name}' from a provided handler: nested bridge calls are not supported. "
                "Use notify for fire-and-forget messages."
            )

        resp_queue = queue.Queue(maxsize=1)

        def on_result(result):
            resp_queue.put((True, result))

        def on_error(error):
            resp_queue.put((False, error))

        msgid = self._pending.register(on_result, on_error)

        try:
            self._send_bytes(protocol.pack_request(msgid, method_name, params))
        except Exception as e:
            self._pending.pop(msgid)
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
            if self._pending.pop(msgid):
                try:
                    self.notify("$/cancelRequest", msgid)
                except Exception:
                    pass
            raise TimeoutError(f"Request '{method_name}' timed out after {timeout}s")
        except Exception:
            self._pending.pop(msgid)  # Ensure the pending entry is cleaned up on any exception path
            raise

    def provide(self, method_name: str, handler):
        """Records the handler and registers it with the router once connected,
        re-registering transparently on every reconnection.
        """
        self._dispatcher.add(method_name, handler)

        if self._is_connected_flag.is_set():
            self._register_with_router("$/register", method_name)

    def unprovide(self, method_name: str):
        """Removes the handler and unregisters it from the router."""
        if self._dispatcher.remove(method_name) is None:
            return  # Nothing to unregister

        if self._is_connected_flag.is_set():
            self._register_with_router("$/unregister", method_name)

    def _register_with_router(self, rpc_method: str, method_name: str):
        """Sends a registration call, in a background thread when invoked from a provided
        handler: handlers must not block on calls.
        """

        def do_call():
            try:
                self.call(rpc_method, method_name)
            except Exception as e:
                logger.error(f"Failed to send '{rpc_method}' for method '{method_name}': {e}")

        if self._dispatcher.on_dispatch_thread():
            threading.Thread(target=do_call, name="Bridge.registration", daemon=True).start()
        else:
            do_call()

    def _conn_manager(self):
        """Alternates between connecting to the router and running the read loop, until stopped."""
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
        """Retries periodically until we have a clean connection. This method **must be** the only
        one allowed to set _is_connected_flag: that keeps the recv path lockless.
        Always entered with no transport published: every path that ends a connection drops it.
        """
        while not self._stop_event.is_set():
            try:
                transport = Transport.connect(self._socket_type, self._peer_addr)
            except Exception as e:
                logger.error(f"Failed to connect to router: {e}")
                self._stop_event.wait(_reconnect_delay)
                continue

            with self._transport_lock:
                self._transport = transport
            self._is_connected_flag.set()

            if self._stop_event.is_set():
                # stop() may have run before the transport was published: undo and bail out
                self._is_connected_flag.clear()
                with self._transport_lock:
                    self._transport = None
                transport.close()
                return

            self._register_provided_methods()
            return

    def _register_provided_methods(self):
        """Registers all provided methods after a (re)connection, in a separate thread:
        each call blocks for a response that only arrives once the read loop runs.
        """
        methods = self._dispatcher.method_names()
        if not methods:
            return

        def register():
            for method in methods:
                try:
                    self.call("$/register", method)
                except Exception as e:
                    logger.error(f"Failed to register method '{method}' after connection: {e}")

        threading.Thread(target=register, name="Bridge.register_methods_on_connect", daemon=True).start()

    def _drop_transport(self, transport):
        """Forces the given transport down so _conn_manager reconnects and resyncs the stream.
        Safe from any thread; a no-op once the transport has been replaced.
        """
        self._is_connected_flag.clear()
        with self._transport_lock:
            if transport is None or self._transport is not transport:
                return  # Already replaced by a newer transport or dropped
            self._transport = None
        transport.close()

    def _read_loop(self):
        """Reads and processes messages until the connection is lost or stop is requested;
        reconnection is the caller's job.
        """
        transport = self._transport
        if transport is None:
            return  # stop() tore the transport down before the read loop started
        unpacker = msgpack.Unpacker(max_buffer_size=self._max_message_size)
        try:
            while not self._stop_event.is_set():
                try:
                    data = transport.recv(4096)
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
            # Tear the transport down so _connect rebuilds it: on a buffer-limit or decode error the
            # socket is still alive but the stream is desynced, but is_alive would call it healthy
            self._drop_transport(transport)
            self._pending.fail_all(ConnectionError("Connection to router lost."))

    def _handle_msg(self, msg: list):
        """Routes one message: requests and notifications to the dispatcher, responses to their pending call."""
        if not msg or not isinstance(msg, list):
            logger.warning("Invalid RPC message received (must be a non-empty list).")
            return

        msg_type = msg[0]
        try:
            if msg_type == protocol.REQUEST:
                msgid, method_name, params = protocol.parse_request(msg)
                handler = self._dispatcher.lookup(method_name)
                if handler is None:
                    self._send_response(
                        msgid, [protocol.FUNCTION_NOT_FOUND_ERR, f"Method not found: '{method_name}'"], None
                    )
                elif not self._dispatcher.submit(handler, method_name, msgid, params):
                    logger.warning(f"Handler queue full, rejecting request for method '{method_name}'")
                    self._send_response(msgid, [protocol.GENERIC_ERR, "Server busy: too many pending requests."], None)

            elif msg_type == protocol.RESPONSE:
                msgid, error, result = protocol.parse_response(msg)
                self._resolve_response(msgid, error, result)

            elif msg_type == protocol.NOTIFICATION:
                method_name, params = protocol.parse_notification(msg)
                handler = self._dispatcher.lookup(method_name)
                # msgid None marks a notification: no response, a full queue drops it
                if handler is not None and not self._dispatcher.submit(handler, method_name, None, params):
                    logger.warning(f"Handler queue full, dropping notification for method '{method_name}'")

            else:
                logger.warning(f"Invalid RPC message type received: {msg_type}")

        except ValueError as ve:
            logger.error(f"Message validation error: {ve}")
        except Exception as e:
            logger.error(f"Unexpected error while handling message: {e}")

    def _resolve_response(self, msgid: int, error: list | None, result):
        """Completes the pending call the response belongs to, if it is still pending."""
        pending = self._pending.pop(msgid)
        if pending is None:
            logger.warning(f"Response for unknown msgid {msgid} received.")
            return

        on_result, on_error = pending
        if error is None:
            on_result(result)
        elif result is not None or error[0] == protocol.ROUTE_ALREADY_EXISTS_ERR:
            # Treat ROUTE_ALREADY_EXISTS_ERR as OK: the router already knows the method, a recoverable situation.
            on_result(result)
        else:
            on_error(error)

    def _send_response(self, msgid: int, err: list | None, response):
        """Packs and sends a response; err is None or an [err_code, err_msg] pair."""
        try:
            # Don't wait for a reconnection: the requester's msgid belongs to the connection that carried it.
            self._send_bytes(protocol.pack_response(msgid, err, response), wait_for_connection=False)
        except ConnectionError:
            pass  # Response sending is best-effort if connection drops while handling request.
        except Exception as e:  # e.g., msgpack encoding error
            logger.error(f"Failed to pack/send response: {e}")

    def _send_bytes(self, packed_data: bytes, wait_for_connection: bool = True):
        """Sends packed data. With wait_for_connection, a disconnected bridge is given a grace
        period to reconnect before failing; otherwise it fails immediately.
        """
        if not self._is_connected_flag.is_set():
            # Wait hoping for an auto-reconnection by _conn_manager.
            if not wait_for_connection or not self._is_connected_flag.wait(timeout=_reconnect_delay):
                raise ConnectionError("Not connected to router, send failed.")

        with self._transport_lock:
            transport = self._transport
        if transport is None:
            raise ConnectionError("No connection object for router, send failed.")

        try:
            transport.send_all(packed_data)
        except OSError as e:
            # A stalled or partial send leaves the stream desynced: drop the transport so it resyncs.
            self._drop_transport(transport)
            raise ConnectionError(f"Send failed due to socket error: {e}")
