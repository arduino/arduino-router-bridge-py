# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

"""MessagePack-RPC wire format: message type tags, error codes, packing and validation."""

import msgpack

# Message type tags defined by the MessagePack-RPC spec
REQUEST = 0
RESPONSE = 1
NOTIFICATION = 2

# Error codes for RPC messages received from the RPC router. These are defined in the RPC router itself.
ROUTE_ALREADY_EXISTS_ERR = 0x05
BUFFER_LIMIT_EXCEEDED_ERR = 0x06

# Error codes for RPC messages sent to Arduino_RouterBridge. These are defined in the lib itself.
MALFORMED_CALL_ERR = 0xFD
FUNCTION_NOT_FOUND_ERR = 0xFE
GENERIC_ERR = 0xFF


def pack_request(msgid: int, method_name: str, params) -> bytes:
    return msgpack.packb([REQUEST, msgid, method_name, params])


def pack_response(msgid: int, error: list | None, result) -> bytes:
    """error is None or an [err_code, err_msg] pair."""
    return msgpack.packb([RESPONSE, msgid, error, result])


def pack_notification(method_name: str, params) -> bytes:
    return msgpack.packb([NOTIFICATION, method_name, params])


# The arduino-router guarantees str-encoded method names; anything else is a malformed message
def _decode_method(method_name) -> str:
    if not isinstance(method_name, str):
        raise ValueError(f"Invalid method name type: {type(method_name)}. Expected str.")
    return method_name


def parse_request(msg: list) -> tuple:
    """Validates a request, returning (msgid, method_name, params)."""
    if len(msg) != 4:
        raise ValueError(f"Invalid RPC request: expected length 4, got {len(msg)}")
    _, msgid, method, params = msg
    if not isinstance(params, (list, tuple)):
        raise ValueError("Invalid RPC request params: expected array or tuple")
    return msgid, _decode_method(method), params


def parse_response(msg: list) -> tuple:
    """Validates a response, returning (msgid, error, result)."""
    if len(msg) != 4:
        raise ValueError(f"Invalid RPC response: expected length 4, got {len(msg)}")
    _, msgid, error, result = msg
    if error and (not isinstance(error, list) or len(error) < 2):
        raise ValueError("Invalid error format in RPC response")
    return msgid, error, result


def parse_notification(msg: list) -> tuple:
    """Validates a notification, returning (method_name, params)."""
    if len(msg) != 3:
        raise ValueError(f"Invalid RPC notification: expected length 3, got {len(msg)}")
    _, method, params = msg
    if not isinstance(params, (list, tuple)):
        raise ValueError("Invalid RPC notification params: expected array or tuple")
    return _decode_method(method), params
