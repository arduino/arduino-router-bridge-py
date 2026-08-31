# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import gc
from unittest.mock import MagicMock

from test_unit_common import UnitTest

from arduino.router_bridge import Bridge


class TestBridgeHandle(UnitTest):
    def test_public_api_delegates_to_engine(self):
        """The Bridge handle must delegate every public operation to its engine."""
        bridge = Bridge()
        bridge._engine = MagicMock()

        bridge.connect()
        bridge._engine.start.assert_called_once()

        bridge.notify("a_method", 1, 2)
        bridge._engine.notify.assert_called_once_with("a_method", 1, 2)

        self.assertEqual(bridge.call("b_method", 3, timeout=5), bridge._engine.call.return_value)
        bridge._engine.call.assert_called_once_with("b_method", 3, timeout=5)

        handler = lambda: None
        bridge.provide("c_method", handler)
        bridge._engine.provide.assert_called_once_with("c_method", handler)

        bridge.unprovide("c_method")
        bridge._engine.unprovide.assert_called_once_with("c_method")

        bridge.wait_connected(timeout=1)
        bridge._engine.wait_connected.assert_called_once_with(1)

        bridge.disconnect()
        bridge._engine.stop.assert_called_once()

    def test_instances_are_independent(self):
        """Each Bridge must own its own engine: no process-wide sharing."""
        first = Bridge()
        second = Bridge()
        self.bridges.extend([first, second])
        self.assertIsNot(first._engine, second._engine)

    def test_context_manager_connects_and_disconnects(self):
        """The bridge can be used as a context manager."""
        bridge = Bridge()
        self.bridges.append(bridge)

        with bridge as entered:
            self.assertIs(entered, bridge)
            self.assertIsNotNone(bridge._engine._read_thread)

        self.assertTrue(bridge._engine._stop_event.is_set())

    def test_garbage_collected_bridge_stops_its_engine(self):
        """An abandoned bridge must disconnect automatically when garbage collected."""
        bridge = Bridge()
        bridge.connect()
        engine = bridge._engine

        del bridge
        gc.collect()

        self.assertTrue(engine._stop_event.is_set())

    def test_reconnect_after_disconnect(self):
        """connect() must work again after disconnect()."""
        bridge = Bridge()
        self.bridges.append(bridge)

        bridge.connect()
        bridge.disconnect()
        self.assertTrue(bridge._engine._stop_event.is_set())

        bridge.connect()
        self.assertFalse(bridge._engine._stop_event.is_set())

    def test_invalid_addresses_are_rejected(self):
        """The constructor must reject unsupported or incomplete addresses."""
        for bad_address in (
            "http://localhost:8080",  # unsupported scheme
            "/var/run/arduino-router.sock",  # missing scheme
            "tcp://localhost",  # missing port
            "tcp://:1234",  # missing host
            "unix://",  # missing path
        ):
            with self.assertRaises(ValueError, msg=f"Address '{bad_address}' was not rejected"):
                Bridge(bad_address)

    def test_address_property(self):
        """The bridge exposes the address it points to."""
        bridge = Bridge("tcp://somehost:4321")
        self.bridges.append(bridge)
        self.assertEqual(bridge.address, "tcp://somehost:4321")
