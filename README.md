# Arduino Router Bridge

A MessagePack-RPC bridge that lets Python applications call methods on an Arduino microcontroller, and expose Python functions the microcontroller can call back.
Requires a Unix or TCP socket managed by the [Arduino Router](https://github.com/arduino/arduino-router) and a compatible board such as the Arduino UNO Q or VENTUNO Q.

## Installation

```bash
pip install arduino-router-bridge
```

## Usage

Create a `Bridge`, connect it, and use it for as long as you need:

```python
from arduino.router_bridge import Bridge

bridge = Bridge()
bridge.connect(timeout=5)  # Waits until connected; True if connected, False on timeout

# Fire-and-forget notification
bridge.notify("set_led", "green", True)

# Blocking call with response
temperature = bridge.call("get_temperature", "sensor1", timeout=5)

bridge.disconnect()
```

It can also be used as a context manager:

```python
with Bridge() as bridge:
    bridge.call("get_temperature", "sensor1")
```

### Exposing Python functions to the microcontroller

```python
def get_country(lon: str, lat: str) -> str:
    return lookup_country(lon, lat)


bridge.provide("get_country", get_country)
```

A provided method can be withdrawn with `bridge.unprovide("get_country")`.

Handlers can be provided before or after connecting: they are registered with the router as soon as the connection is available and re-registered transparently whenever it is re-established. Handlers run sequentially on a dedicated thread. A handler may send notifications, but must not call back into the bridge with `call()`: the peer may be blocked waiting for the handler's own response, so nested calls risk deadlocks and request loops and are rejected with a `RuntimeError`.

## Configuration

The bridge connects to the Arduino RPC router at `unix:///var/run/arduino-router.sock` by default. Pass an `address` to the constructor to connect elsewhere.

`unix://<path>` is the standard transport. It is only available on Linux, where the router runs and manages the socket; constructing a bridge with a `unix://` address on a platform without unix socket support raises `ValueError`. `tcp://<host>:<port>` is meant for development and debugging only: it is unauthenticated and unencrypted (see the security model below), and by default the router does not expose it to external hosts.

Instances are independent: create one per router you need to talk to. How an instance is shared is the caller's concern; an embedding runtime that needs a process-wide bridge creates one instance at startup and exposes it itself:

```python
from arduino.router_bridge import Bridge

bridge = Bridge()  # Uses the default address unix:///var/run/arduino-router.sock
bridge.connect()
```

`connect()` waits until the connection is established, indefinitely unless a `timeout` is given, and returns whether it succeeded; on timeout the bridge keeps connecting in the background. A lost connection is re-established automatically. Disconnect explicitly (or use a context manager) when done; as a safety net, a garbage-collected bridge disconnects automatically.

## Security model

The router socket is the trust boundary: any process that can connect to it can invoke the provided methods and forge RPC responses. Unix sockets are protected by file permissions, managed by the Arduino Router. `tcp://` connections carry no authentication or encryption: use them only on localhost or an isolated, trusted network, and never expose them to untrusted hosts.

Handler exceptions are reported to the caller by exception type only; full details, including the traceback, stay in the local log. To bound memory usage, incoming messages are capped at 1 MiB and pending handler executions at 1024 by default; both limits are configurable per `Bridge` (`max_message_size`, `max_pending_handlers`).

## Logging

The library logs through the standard `logging` module under the `arduino.router_bridge` namespace and emits nothing unless the application configures a handler:

```python
import logging

logging.getLogger("arduino.router_bridge").addHandler(logging.StreamHandler())
```

## License

MPL-2.0
