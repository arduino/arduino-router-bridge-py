# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import unittest

import msgpack

from arduino.router_bridge import protocol


class TestPacking(unittest.TestCase):
    def test_pack_request(self):
        """A request packs to [0, msgid, method, params]."""
        packed = protocol.pack_request(7, "get_value", ["sensor1", 2])
        self.assertEqual(msgpack.unpackb(packed), [0, 7, "get_value", ["sensor1", 2]])

    def test_pack_response(self):
        """A response packs to [1, msgid, error, result]."""
        packed = protocol.pack_response(7, None, 42)
        self.assertEqual(msgpack.unpackb(packed), [1, 7, None, 42])

        packed = protocol.pack_response(7, [protocol.GENERIC_ERR, "boom"], None)
        self.assertEqual(msgpack.unpackb(packed), [1, 7, [protocol.GENERIC_ERR, "boom"], None])

    def test_pack_notification(self):
        """A notification packs to [2, method, params]."""
        packed = protocol.pack_notification("set_led", ("green", True))
        self.assertEqual(msgpack.unpackb(packed), [2, "set_led", ["green", True]])


class TestParseRequest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(protocol.parse_request([0, 7, "add", [1, 2]]), (7, "add", [1, 2]))

    def test_wrong_length(self):
        with self.assertRaises(ValueError) as cm:
            protocol.parse_request([0, 7, "add", [1, 2], "extra field"])
        self.assertEqual(str(cm.exception), "Invalid RPC request: expected length 4, got 5")

    def test_params_not_a_sequence(self):
        with self.assertRaises(ValueError) as cm:
            protocol.parse_request([0, 7, "add", 1])
        self.assertEqual(str(cm.exception), "Invalid RPC request params: expected array or tuple")

    def test_bytes_method_name_rejected(self):
        """A non-string method name is rejected as malformed: the router guarantees str."""
        with self.assertRaises(ValueError) as cm:
            protocol.parse_request([0, 7, b"add", []])
        self.assertIn("Invalid method name type", str(cm.exception))


class TestParseResponse(unittest.TestCase):
    def test_valid_result(self):
        self.assertEqual(protocol.parse_response([1, 7, None, "ok"]), (7, None, "ok"))

    def test_valid_error(self):
        error = [protocol.GENERIC_ERR, "boom"]
        self.assertEqual(protocol.parse_response([1, 7, error, None]), (7, error, None))

    def test_wrong_length(self):
        with self.assertRaises(ValueError) as cm:
            protocol.parse_response([1, 7, None, "ok", "extra field"])
        self.assertEqual(str(cm.exception), "Invalid RPC response: expected length 4, got 5")

    def test_malformed_error(self):
        for bad_error in (42, [protocol.GENERIC_ERR], "boom"):
            with self.assertRaises(ValueError) as cm:
                protocol.parse_response([1, 7, bad_error, None])
            self.assertEqual(str(cm.exception), "Invalid error format in RPC response")


class TestParseNotification(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(protocol.parse_notification([2, "set_led", ["green"]]), ("set_led", ["green"]))

    def test_wrong_length(self):
        with self.assertRaises(ValueError) as cm:
            protocol.parse_notification([2, "set_led", ["green"], "extra field"])
        self.assertEqual(str(cm.exception), "Invalid RPC notification: expected length 3, got 4")

    def test_params_not_a_sequence(self):
        with self.assertRaises(ValueError) as cm:
            protocol.parse_notification([2, "set_led", 42])
        self.assertEqual(str(cm.exception), "Invalid RPC notification params: expected array or tuple")

    def test_bytes_method_name_rejected(self):
        with self.assertRaises(ValueError) as cm:
            protocol.parse_notification([2, b"set_led", []])
        self.assertIn("Invalid method name type", str(cm.exception))
