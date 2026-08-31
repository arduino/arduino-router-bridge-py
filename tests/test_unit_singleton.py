# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

from test_unit_common import UnitTest

from arduino.router_bridge import bridge as bridge_module
from arduino.router_bridge.bridge import Bridge, ClientServer, call, notify, provide, shutdown


class TestSharedInstances(UnitTest):
    def test_same_address_shares_the_instance(self):
        """Requesting the same address twice must return the same connection instance."""
        first = ClientServer()
        second = ClientServer()
        explicit_default = ClientServer(address="unix:///var/run/arduino-router.sock")
        self.assertIs(first, second)
        self.assertIs(first, explicit_default)

    def test_different_addresses_get_different_instances(self):
        """Requesting different addresses must return independent connection instances."""
        first = ClientServer(address="unix:///tmp/a.sock")
        second = ClientServer(address="unix:///tmp/b.sock")
        self.assertIsNot(first, second)
        self.assertEqual(len(bridge_module._instances), 2)

    def test_bridge_connect_binds_unaddressed_lookups(self):
        """Bridge.connect must bind the connection used by Bridge and the bare decorators."""
        bound = Bridge.connect("unix:///tmp/app.sock")

        self.assertIs(ClientServer(), bound)

        @notify()
        def set_led(color: str, status: bool): ...

        set_led_instance = MagicMock()
        with patch.dict(bridge_module._instances, {"unix:///tmp/app.sock": set_led_instance}):
            set_led("green", True)
            set_led_instance.notify.assert_called_once_with("set_led", "green", True)

    def test_bridge_connect_default_resolves_at_invocation_time(self):
        """A bare decorator applied before Bridge.connect must still use the bound address."""

        @notify()
        def set_led(status: bool): ...

        Bridge.connect("unix:///tmp/late.sock")  # Bound after decoration

        instance = MagicMock()
        with patch.dict(bridge_module._instances, {"unix:///tmp/late.sock": instance}):
            set_led(True)
            instance.notify.assert_called_once_with("set_led", True)

    def test_shutdown_stops_and_forgets_instances(self):
        """shutdown() must stop the shared connections and allow fresh ones to be created."""
        client = ClientServer()
        client.stop = MagicMock()

        shutdown()

        client.stop.assert_called_once()
        self.assertEqual(len(bridge_module._instances), 0)
        self.assertIsNot(ClientServer(), client)  # A fresh instance is created on next use

    def test_shutdown_single_address(self):
        """shutdown(address) must only stop the matching shared connection."""
        target = ClientServer(address="unix:///tmp/a.sock")
        other = ClientServer(address="unix:///tmp/b.sock")
        target.stop = MagicMock()
        other.stop = MagicMock()

        shutdown("unix:///tmp/a.sock")

        target.stop.assert_called_once()
        other.stop.assert_not_called()
        self.assertIs(ClientServer(address="unix:///tmp/b.sock"), other)

    def test_invalid_address_raises_at_lookup(self):
        """An invalid address must be rejected when the shared connection is requested."""
        with self.assertRaises(ValueError):
            ClientServer(address="http://localhost:8080")


class TestBridgeFacade(UnitTest):
    def test_bridge_routes_to_the_shared_instance(self):
        """Bridge static methods must route to the default shared connection."""
        instance = MagicMock()
        with patch("arduino.router_bridge.bridge._get_instance", return_value=instance) as get_instance:
            Bridge.notify("a_method", 1, 2)
            instance.notify.assert_called_once_with("a_method", 1, 2)

            Bridge.call("b_method", 3, timeout=5)
            instance.call.assert_called_once_with("b_method", 3, timeout=5)

            handler = lambda: None
            Bridge.provide("c_method", handler)
            instance.provide.assert_called_once_with("c_method", handler)

            Bridge.unprovide("c_method")
            instance.unprovide.assert_called_once_with("c_method")

            get_instance.assert_called_with()  # Always the default address


class TestDecorators(UnitTest):
    def test_notify_and_call_decorators_are_lazy(self):
        """Decorating with @notify/@call must not create any connection."""

        @notify()
        def set_led(color: str, status: bool): ...

        @call("math.add", timeout=3)
        def add(a: int, b: int) -> int: ...

        self.assertEqual(len(bridge_module._instances), 0)

    def test_notify_decorator_sends_notification_on_invocation(self):
        """Calling a @notify function must send the RPC notification through the shared connection."""
        instance = MagicMock()
        with patch("arduino.router_bridge.bridge._get_instance", return_value=instance):

            @notify("custom.name")
            def set_led(color: str, status: bool): ...

            set_led("green", True)
            instance.notify.assert_called_once_with("custom.name", "green", True)

            with self.assertRaises(TypeError):
                set_led(color="green", status=True)  # Keyword args are not supported

    def test_call_decorator_calls_on_invocation(self):
        """Calling a @call function must send the RPC call and honor the timeout override."""
        instance = MagicMock(**{"call.return_value": 42})
        with patch("arduino.router_bridge.bridge._get_instance", return_value=instance):

            @call(timeout=3)
            def add(a: int, b: int) -> int: ...

            self.assertEqual(add(1, 2), 42)
            instance.call.assert_called_once_with("add", 1, 2, timeout=3)
            instance.call.reset_mock()

            add(1, 2, timeout=7)  # Per-invocation override
            instance.call.assert_called_once_with("add", 1, 2, timeout=7)

            with self.assertRaises(TypeError):
                add(a=1, b=2)  # Keyword args are not supported

    def test_call_decorator_timeout_none_waits_indefinitely(self):
        """@call(timeout=None) must pass timeout=None through to the connection."""
        instance = MagicMock()
        with patch("arduino.router_bridge.bridge._get_instance", return_value=instance):

            @call(timeout=None)
            def get_status() -> str: ...

            get_status()
            instance.call.assert_called_once_with("get_status", timeout=None)

    def test_provide_decorator_registers_handler_without_connection(self):
        """@provide must record the handler even when the router is not reachable."""

        @provide("custom.rpc.name")
        def get_country(lon: str, lat: str) -> str:
            return "IT"

        instance = bridge_module._instances.get("unix:///var/run/arduino-router.sock")
        self.assertIsNotNone(instance)
        self.assertIs(instance.handlers["custom.rpc.name"], get_country)

    def test_decorators_reject_methods(self):
        """Decorating a method or classmethod must fail at decoration time."""
        for decorator in (notify(), call(), provide()):
            with self.assertRaises(TypeError):

                @decorator
                def fake_method(self, value): ...
