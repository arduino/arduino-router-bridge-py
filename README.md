# Arduino Router Bridge

A MessagePack-RPC bridge that lets Python applications call methods on an Arduino
microcontroller, and expose Python functions the microcontroller can call back.
Requires a Unix or TCP socket managed by the [Arduino Router](https://github.com/arduino/arduino-router) and a compatible board such as the Arduino UNO Q or VENTUNO Q.

## Installation

```bash
pip install arduino-router-bridge
```

## Usage

### Calling the microcontroller

```python
from arduino.router_bridge import Bridge

# Fire-and-forget notification
Bridge.notify("set_led", "green", True)

# Blocking call with response
temperature = Bridge.call("get_temperature", "sensor1", timeout=5)
```

### Exposing Python functions to the microcontroller

```python
from arduino.router_bridge import Bridge


def get_country(lon: str, lat: str) -> str:
    return lookup_country(lon, lat)


Bridge.provide("get_country", get_country)
```

### Decorator API

```python
from arduino.router_bridge import notify, call, provide


@call("math.add", timeout=3)
def add(a: int, b: int) -> int: ...  # Body is not needed


@notify()
def set_led(color: str, status: bool): ...


@provide()
def get_status() -> str:
    return "ok"


result = add(1, 2)  # Sends the "math.add" RPC call and returns its response
set_led("green", True)  # Sends the "set_led" RPC notification
```

## Configuration

The bridge connects to the Arduino RPC router at `unix:///var/run/arduino-router.sock`
by default. Pass an `address` to the decorators to connect elsewhere; both
`unix://<path>` and `tcp://<host>:<port>` addresses are supported.

Embedding frameworks can bind the address used when none is given explicitly, once
at startup and before application code runs:

```python
import os

from arduino.router_bridge import Bridge

Bridge.connect(os.environ["APP_SOCKET"])
```

`Bridge`, the decorators and `ClientServer` share one process-wide connection per
address, established lazily in the background and reconnected automatically:
provided methods are registered as soon as the connection is available and
re-registered transparently whenever it is re-established. Call `shutdown()` to
stop the shared connections, for example on application exit.

### Multiple routers

Applications that need full control over the connection lifecycle, or independent
connections to several routers, can use `BridgeConnection` directly:

```python
from arduino.router_bridge import BridgeConnection

with BridgeConnection("tcp://192.168.1.10:5000") as conn:
    conn.wait_connected(timeout=5)
    conn.call("get_temperature", "sensor1")
```

## Security model

The router socket is the trust boundary: any process that can connect to it can
invoke the provided methods and forge RPC responses. Unix sockets are protected by
file permissions, managed by the Arduino Router. `tcp://` connections carry no
authentication or encryption: use them only on localhost or an isolated, trusted
network, and never expose them to untrusted hosts.

Handler exceptions are reported to the caller by exception type only; full details,
including the traceback, stay in the local log. To bound memory usage, incoming
messages are capped at 1 MiB and pending handler executions at 1024 by default;
both limits are configurable per `BridgeConnection`.

## Logging

The library logs through the standard `logging` module under the
`arduino.router_bridge` namespace and emits nothing unless the application
configures a handler:

```python
import logging

logging.getLogger("arduino.router_bridge").addHandler(logging.StreamHandler())
```

## License

MPL-2.0
