# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-31

### Added

- MessagePack-RPC bridge between Python apps and Arduino microcontrollers:
  `Bridge` client/server, `@call`, `@notify` and `@provide` decorators.
- Configurable router address resolution via `set_address_resolver`.
- Pluggable logging via `set_logger`; silent by default under the
  `arduino.router_bridge` logger namespace.
- Type hints shipped with the package (`py.typed`).

[Unreleased]: https://github.com/arduino/arduino-router-bridge-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/arduino/arduino-router-bridge-py/releases/tag/v0.1.0
