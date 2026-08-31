# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Public `BridgeConnection` class for multi-instance use: independent connections with an explicit lifecycle, usable as a context manager.
- `Bridge.connect(address)` to bind the address used by `Bridge` and the decorators when none is given explicitly; embedding runtimes should call it once at startup.
- `wait_connected()` to wait for the connection to be established.
- `shutdown()` to stop the process-wide shared connections.
- `DEFAULT_ADDRESS` constant with the default router address.
- Invalid or incomplete router addresses are rejected with a `ValueError` at creation instead of retrying forever in the background.

### Changed

- `Bridge`, the decorators and `ClientServer` now share one connection per address: the `address` argument selects the shared connection instead of being silently ignored after the first use. `ClientServer` is now a factory function returning the shared `BridgeConnection`.
- Connecting no longer blocks: `start()` returns immediately and the connection is established and retried in the background. Decorating with `@notify`/`@call` no longer opens a connection at import time.
- `provide` registration is declarative: handlers are recorded immediately and registered with the router as soon as a connection is available, then re-registered on every reconnection. `provide`/`unprovide` no longer raise if the router is unreachable.
- `@call(timeout=None)` and `Bridge.call(timeout=None)` now wait indefinitely as documented.
- Provided handlers now run sequentially on a dedicated dispatcher thread instead of the read thread: a handler can call back into the bridge, and a slow handler no longer stalls response processing.
- `notify` is now truly fire-and-forget: when the router is disconnected it drops the notification immediately instead of blocking up to the reconnection delay.

### Removed

- `set_address_resolver`: bind the default address directly with `Bridge.connect(address)` instead; explicit `address` arguments are no longer remapped.
- `set_logger`: attach a handler or set a level on the `arduino.router_bridge` logger namespace instead.

### Security

- Handler exceptions are reported to the peer by exception type only; the message and traceback stay in the local log.
- Incoming messages are capped at 1 MiB and queued handler executions at 1024 by default, bounding memory usage; both limits are configurable per `BridgeConnection`.
- The trust model is now documented: the router socket is the boundary, and `tcp://` carries no authentication or encryption.

### Fixed

- Calls pending when the connection drops or the bridge is stopped now raise a `ConnectionError` instead of an internal `TypeError`.
- The read loop no longer spins at full CPU on unexpected socket errors: any read error now triggers a reconnection.
- The connection status check no longer relies on `MSG_DONTWAIT`, which is unavailable on Windows.
- The connected flag is cleared before a broken connection is torn down, so concurrent sends can no longer observe a connected state without a usable socket.
- `stop()` can no longer hang behind a send blocked mid-transfer: socket writes are serialized by a dedicated lock and the socket is shut down independently of it.
- Concurrent `start()`/`stop()` calls are serialized: a race can no longer spawn duplicate background threads.
- Blocking I/O is no longer performed while holding internal locks: request cancellation on timeout and handler re-registration after reconnect no longer stall response dispatching or `provide`/`unprovide`.
- Message IDs are reserved atomically with their response callbacks, removing a reuse race on wrap-around.

## [0.1.0] - 2026-08-31

### Added

- MessagePack-RPC bridge between Python apps and Arduino microcontrollers: `Bridge` client/server, `@call`, `@notify` and `@provide` decorators.
- Configurable router address resolution via `set_address_resolver`.
- Pluggable logging via `set_logger`; silent by default under the
  `arduino.router_bridge` logger namespace.
- Type hints shipped with the package (`py.typed`).

[Unreleased]: https://github.com/arduino/arduino-router-bridge-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/arduino/arduino-router-bridge-py/releases/tag/v0.1.0
