# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import inspect
import threading
from functools import wraps

from .connection import DEFAULT_ADDRESS, BridgeConnection

__all__ = [
    "Bridge",
    "ClientServer",
    "notify",
    "call",
    "provide",
    "shutdown",
]


_default_address = DEFAULT_ADDRESS  # Address used when none is given; rebound by Bridge.connect()

# Process-wide shared connections, one per address
_instances: dict[str, BridgeConnection] = {}
_instances_lock = threading.Lock()


def _get_instance(address: str | None = None) -> BridgeConnection:
    """Returns the process-wide shared connection for the given address (the default
    address if None), creating and starting it on first use. Restarts it if it was stopped.
    """
    if address is None:
        address = _default_address
    with _instances_lock:
        instance = _instances.get(address)
        if instance is None:
            instance = BridgeConnection(address)
            _instances[address] = instance
        instance.start()  # No-op when already running
        return instance


def ClientServer(address: str | None = None) -> BridgeConnection:
    """Returns the process-wide shared connection for the given address, starting it if needed.
    Uses the default address (see `Bridge.connect`) if none is given.

    All callers requesting the same address share the same `BridgeConnection` instance.
    Provided for embedding runtimes; application code should prefer `Bridge` or the decorators.
    """
    return _get_instance(address)


def shutdown(address: str | None = None):
    """Stops the process-wide shared connection for the given address and forgets it,
    or all shared connections if no address is given. A later use of `Bridge`, the
    decorators or `ClientServer` transparently creates a fresh connection.

    Args:
        address (str, optional): The address whose shared connection should be stopped.
            All shared connections are stopped if None.
    """
    with _instances_lock:
        if address is None:
            to_stop = list(_instances.values())
            _instances.clear()
        else:
            instance = _instances.pop(address, None)
            to_stop = [instance] if instance is not None else []
    for instance in to_stop:
        instance.stop()


class Bridge:
    @staticmethod
    def connect(address: str = DEFAULT_ADDRESS) -> BridgeConnection:
        """Binds the router address used by `Bridge` and the decorators when none is
        given explicitly, and returns its shared connection.

        Returns immediately: the connection is established in the background and
        retried until it succeeds. Without this call, `Bridge` and the decorators
        connect to the default address on first use. Embedding runtimes should call
        it once at startup, before application code runs.

        Args:
            address (str): The address of the microcontroller router to connect to,
                either "unix://<path>" or "tcp://<host>:<port>".

        Raises:
            ValueError: If the address scheme is not supported or the address is incomplete.

        Examples:
            Bridge.connect(f"unix://{app_socket_path}")
        """
        global _default_address
        _default_address = address
        return _get_instance(address)

    @staticmethod
    def notify(method_name: str, *params):
        """Sends a notification to the microcontroller without waiting for a response.

        Args:
            method_name (str): The name of the method to notify on the microcontroller.
            *params: The parameters to pass to the method.

        Examples:
            Bridge.notify("set_led", "green", True)
            Bridge.notify("log_message", "Hello, microcontroller!")
        """
        _get_instance().notify(method_name, *params)

    @staticmethod
    def call(method_name: str, *params, timeout: float | None = 10):
        """Calls a method on the microcontroller and waits for a response.
        Raises an exception if the call fails or times out.

        Args:
            method_name (str): The name of the method to call on the microcontroller.
            *params: The parameters to pass to the method.
            timeout (float, optional): The maximum time to wait for a response in seconds.
                If None, waits indefinitely. Defaults to 10s.

        Raises:
            ValueError: If the method does not exist.
            TimeoutError: If the call takes more time than the specified timeout.
            RuntimeError: If the call fails unexpectedly.

        Examples:
            temperature = Bridge.call("get_temperature", "sensor1")
            print(f"Temperature: {temperature}")
        """
        return _get_instance().call(method_name, *params, timeout=timeout)

    @staticmethod
    def provide(method_name: str, handler: callable):
        """Makes a method available to the microcontroller, so it can call it remotely.
        The handler should be a callable that can take arguments.

        The handler is registered with the router as soon as a connection is available
        and re-registered transparently on every reconnection.

        Args:
            method_name (str): The name under which the function should be provided to the microcontroller.
            handler (callable): The function to call when the microcontroller requires it.

        Raises:
            ValueError: If handler is not callable.

        Examples:
            def get_country(lon: str, lat: str) -> str:
                ... lookup country by lon and lat ...
                return country_name

            Bridge.provide("get_country", get_country)
        """
        _get_instance().provide(method_name, handler)

    @staticmethod
    def unprovide(method_name: str):
        """Makes a method no more available to the microcontroller.

        Args:
            method_name (str): The name under which the function is already provided to the microcontroller.

        Examples:
            Bridge.unprovide("get_country")
        """
        _get_instance().unprovide(method_name)


def notify(method_name: str | None = None, address: str | None = None):
    """Decorator that transforms a function into a notification for the microcontroller.

    When the decorated function is called, an RPC 'notify' (fire-and-forget) is sent
    to the microcontroller. The notify's arguments are taken from the decorated function's arguments.
    The RPC method name defaults to the decorated function's name if not specified.
    The connection is established lazily, on the first invocation of the decorated function.

    Args:
        method_name (str, optional): The name of the RPC method to call. Defaults to the decorated function's name.
        address (str, optional): The address of the microcontroller router to connect to. Can be a TCP socket or a Unix socket. Defaults to the address bound via `Bridge.connect`, or DEFAULT_ADDRESS.

    Raises:
        TypeError: If the decorated function is called with unexpected keyword arguments.

    Examples:
        @notify()
        def set_led(color: str, status: bool): ... # Body is not needed

        @notify("leds.green.set_status")
        def set_green_led(status: bool): ...

        set_led("green", True) # Sends "set_led" RPC notification
        set_green_led(True) # Sends "leds.green.set_status" RPC notification
    """

    def decorator(func):
        actual_method_name = method_name if method_name is not None else func.__name__

        if _is_unbound_or_class_method(func):
            raise TypeError(f"'{func.__name__}' is expected to be a function but is a method or a classmethod.")

        @wraps(func)
        def wrapper(*args, **kwargs):
            # Any kwargs passed to the decorated function are unexpected.
            if kwargs:
                raise TypeError(f"Unexpected {list(kwargs.keys())} keyword args: only positional args are supported.")

            _get_instance(address).notify(actual_method_name, *args)

        return wrapper

    return decorator


def call(method_name: str | None = None, timeout: float | None = 10, address: str | None = None):
    """Decorator that transforms a function into an RPC call to the microcontroller.

    When the decorated function is called, an RPC 'call' (request and response) is sent
    to the microcontroller. The call's arguments are taken from the decorated function's arguments.
    The RPC method name defaults to the decorated function's name if not specified.
    A default timeout for the RPC call can be set via the decorator but it can be overridden
    by passing a 'timeout' keyword argument when calling the decorated function.
    The connection is established lazily, on the first invocation of the decorated function.

    Args:
        method_name (str, optional): The name of the RPC method to call. Defaults to the decorated function's name.
        timeout (float, optional): The maximum time to wait for a response in seconds. If None, waits indefinitely. Defaults to 10s.
        address (str, optional): The address of the microcontroller router to connect to. Can be a TCP socket or a Unix socket. Defaults to the address bound via `Bridge.connect`, or DEFAULT_ADDRESS.

    Raises:
        TypeError: If the decorated function is called with unexpected keyword arguments.
        ValueError: If the method does not exist.
        TimeoutError: If the call takes more time than the specified timeout.
        RuntimeError: If the call fails unexpectedly.

    Examples:
        @call()
        def get_led(color: str) -> bool: ... # Body is not needed

        @call("leds.green.status", timeout=3)
        def get_green_led() -> bool: ...

        state = get_led("green")
        state = get_green_led()
    """

    def decorator(func):
        actual_method_name = method_name if method_name is not None else func.__name__

        if _is_unbound_or_class_method(func):
            raise TypeError(f"'{func.__name__}' is expected to be a function but is a method or a classmethod.")

        @wraps(func)
        def wrapper(*args, **kwargs):
            # An optional 'timeout' keyword overrides the decorator's default
            actual_timeout = kwargs.pop("timeout", timeout)

            # Any remaining kwargs passed to the decorated function are unexpected.
            if kwargs:
                raise TypeError(f"Unexpected {list(kwargs.keys())} keyword args: only positional args are supported.")

            return _get_instance(address).call(actual_method_name, *args, timeout=actual_timeout)

        return wrapper

    return decorator


def provide(method_name: str | None = None, address: str | None = None):
    """Decorator that makes a method available to the microcontroller, so it can call it remotely.

    The decorated function is automatically registered using its own name as method name,
    unless `method_name` is provided. The registration with the router happens as soon as
    a connection is available and is renewed transparently on every reconnection.

    Args:
        method_name (str, optional): The name under which the function should be registered.
        address (str, optional): The address of the microcontroller router to connect to. Can be a TCP socket or a Unix socket. Defaults to the address bound via `Bridge.connect`, or DEFAULT_ADDRESS.

    Examples:
        @provide()
        def get_country(lon: str, lat: str) -> str:
            ... lookup country by lon and lat ...
            return country_name

        @provide("custom.rpc.name")
        def another_handler(param):
            ... logic ...
    """

    def decorator(func):
        actual_method_name = method_name if method_name is not None else func.__name__

        if _is_unbound_or_class_method(func):
            raise TypeError(f"'{func.__name__}' is expected to be a function but is a method or a classmethod.")

        _get_instance(address).provide(actual_method_name, func)

        # Return the original function, registration is only a side-effect
        return func

    return decorator


# Helper that implements a heuristic to determine if a function is a method (unbound) or @classmethod
def _is_unbound_or_class_method(func):
    try:
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if not params:
            return False
        first_param = params[0]
        return first_param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ) and first_param.name in ("self", "cls")
    except ValueError:
        return False
