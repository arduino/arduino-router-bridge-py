# Arduino App Bridge

A MessagePack-RPC bridge that lets Python applications call methods on an Arduino
microcontroller, and expose Python functions the microcontroller can call back —
over a Unix or TCP socket managed by the Arduino RPC router.

## Installation

```bash
pip install arduino-app-bridge
```

## Usage

### Calling the microcontroller

```python
from arduino.app_bridge import Bridge

# Fire-and-forget notification
Bridge.notify("set_led", "green", True)

# Blocking call with response
temperature = Bridge.call("get_temperature", "sensor1", timeout=5)
```

### Exposing Python functions to the microcontroller

```python
from arduino.app_bridge import Bridge


def get_country(lon: str, lat: str) -> str:
    return lookup_country(lon, lat)


Bridge.provide("get_country", get_country)
```

### Decorator API

```python
from arduino.app_bridge import notify, call, provide


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

Embedding frameworks can install a resolver that maps the requested address to the
effective one whenever a connection is created:

```python
import os

from arduino.app_bridge import set_address_resolver

set_address_resolver(lambda address: os.environ.get("APP_SOCKET", address))
```

The connection is a process-wide singleton with automatic reconnection: provided
methods are re-registered transparently whenever the connection is re-established.

## Logging

The library logs through the standard `logging` module under the
`arduino.app_bridge` namespace and emits nothing unless the application
configures a handler:

```python
import logging

logging.getLogger("arduino.app_bridge").addHandler(logging.StreamHandler())
```

Embedding frameworks can also inject a pre-configured logger:

```python
from arduino.app_bridge import set_logger

set_logger(my_logger)
```

## License

MPL-2.0
