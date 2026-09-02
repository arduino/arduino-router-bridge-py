# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

"""The public Bridge handle: a thin facade over the internal connection."""

import weakref

from .connection import _BridgeConnection
from .transport import DEFAULT_ADDRESS


class Bridge:
    """A MessagePack-RPC bridge to an Arduino Router.

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
                requests are rejected as busy and further notifications dropped. Also bounds memory:
                at most this many queued messages, each up to max_message_size. Defaults to 1024.

        Raises:
            ValueError: If the address scheme is not supported, the address is incomplete, or
                unix:// is used on a platform without unix socket support.
        """
        self._connection = _BridgeConnection(address, max_message_size, max_pending_handlers)
        # The connection never references the Bridge instance, collecting an abandoned instance stops it.
        weakref.finalize(self, self._connection._signal_stop)

    @property
    def address(self) -> str:
        """The router address this bridge points to."""
        return self._connection.address

    def connect(self):
        """Starts connecting to the router in the background and returns immediately.
        The connection is retried until it succeeds (see ``wait_connected``) and
        re-established automatically whenever it is lost. A no-op if already running.
        """
        self._connection.start()

    def disconnect(self):
        """Closes the connection and releases resources. Idempotent and safe to call
        even if ``connect()`` was never called; ``connect()`` can be called again afterwards.
        """
        self._connection.stop()

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Waits until the connection to the router is established.

        Args:
            timeout (float, optional): Maximum time to wait in seconds. Waits indefinitely if None.

        Returns:
            bool: True if connected, False if the timeout expired first.
        """
        return self._connection.wait_connected(timeout)

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
        self._connection.notify(method_name, *params)

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
        return self._connection.call(method_name, *params, timeout=timeout)

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
        self._connection.provide(method_name, handler)

    def unprovide(self, method_name: str):
        """Makes a method no more available to the microcontroller.

        Args:
            method_name (str): The name under which the function is already provided to the microcontroller.

        Examples:
            bridge.unprovide("get_country")
        """
        self._connection.unprovide(method_name)
