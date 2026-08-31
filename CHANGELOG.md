# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-01

### Added

- Context manager support and automatic disconnection when a bridge is garbage collected; explicit `disconnect()` remains the recommended path.
- `wait_connected()` to wait until the connection is established.
- `address` property exposing the router address a bridge points to.
- `DEFAULT_ADDRESS` constant with the default router address.
- Configurable resource limits per bridge: `max_message_size` and `max_pending_handlers`.

### Changed

- The API is now instance-based and agnostic to the instantiation model: create a `Bridge` per router, `connect()`, use it, `disconnect()`. How instances are shared is the caller's concern; an embedding runtime that needs a process-wide bridge creates one instance and exposes it itself.
- Connecting no longer blocks: `connect()` returns immediately and the connection is established and retried in the background.
- Invalid or incomplete router addresses are rejected with a `ValueError` at creation instead of retrying forever in the background.
- `provide` registration is declarative: handlers are recorded immediately and registered with the router as soon as a connection is available, then re-registered on every reconnection. `provide`/`unprovide` no longer raise if the router is unreachable.
- Provided handlers now run sequentially on a dedicated dispatcher thread instead of the read thread: a slow handler no longer stalls response processing.
- Handlers may send notifications but must not call back into the bridge: nested calls from handlers are rejected with a `RuntimeError`, preventing deadlocks with a peer blocked on the handler's response and unbounded request loops.
- `notify` is now truly fire-and-forget: when the router is disconnected it drops the notification immediately instead of blocking up to the reconnection delay.
- `call(timeout=None)` now waits indefinitely as documented.

### Removed

- The process-wide singleton layer: the static `Bridge` facade, the `@notify`/`@call`/`@provide` decorators, `ClientServer` and the per-address connection pool. Use a `Bridge` instance instead.
- `set_address_resolver`: pass the address to the `Bridge` constructor instead.
- `set_logger`: attach a handler or set a level on the `arduino.router_bridge` logger namespace instead.
- The RPC error-code constants are no longer exported: they never reach callers in a structured form.

### Fixed

- Calls pending when the connection drops or the bridge is stopped now raise a `ConnectionError` instead of an internal `TypeError`.
- The read loop no longer spins at full CPU on unexpected socket errors: any read error now triggers a reconnection.
- The connection status check no longer relies on `MSG_DONTWAIT`, which is unavailable on Windows.
- The connected flag is cleared before a broken connection is torn down, so concurrent sends can no longer observe a connected state without a usable socket.
- `disconnect()` can no longer hang behind a send blocked mid-transfer: socket writes are serialized by a dedicated lock and the socket is shut down independently of it.
- Concurrent `connect()`/`disconnect()` calls are serialized: a race can no longer spawn duplicate background threads.
- Blocking I/O is no longer performed while holding internal locks: request cancellation on timeout and handler re-registration after reconnect no longer stall response dispatching or `provide`/`unprovide`.
- Message IDs are reserved atomically with their response callbacks, removing a reuse race on wrap-around.

### Security

- Handler exceptions are reported to the peer by exception type only; the message and traceback stay in the local log.
- Incoming messages are capped at 1 MiB and queued handler executions at 1024 by default, bounding memory usage; both limits are configurable per `Bridge`.
- Method names are strictly validated: non-string names are rejected as malformed instead of being decoded from bytes (verified against the firmware library and the router, which always encode them as msgpack str).
- The trust model is now documented: the router socket is the boundary, and `tcp://` carries no authentication or encryption.

## [0.1.0] - 2026-08-31

### Added

- MessagePack-RPC bridge between Python apps and Arduino microcontrollers: `Bridge` client/server, `@call`, `@notify` and `@provide` decorators.
- Configurable router address resolution via `set_address_resolver`.
- Pluggable logging via `set_logger`; silent by default under the
  `arduino.router_bridge` logger namespace.
- Type hints shipped with the package (`py.typed`).

[Unreleased]: https://github.com/arduino/arduino-router-bridge-py/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/arduino/arduino-router-bridge-py/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/arduino/arduino-router-bridge-py/releases/tag/v0.1.0
