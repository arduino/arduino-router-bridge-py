# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import logging

from .bridge import (
    Bridge,
    ClientServer,
    notify,
    call,
    provide,
    set_address_resolver,
    set_logger,
    ROUTE_ALREADY_EXISTS_ERR,
    BUFFER_LIMIT_EXCEEDED_ERR,
    MALFORMED_CALL_ERR,
    FUNCTION_NOT_FOUND_ERR,
    GENERIC_ERR,
)

__all__ = [
    "Bridge",
    "ClientServer",
    "notify",
    "call",
    "provide",
    "set_address_resolver",
    "set_logger",
    "ROUTE_ALREADY_EXISTS_ERR",
    "BUFFER_LIMIT_EXCEEDED_ERR",
    "MALFORMED_CALL_ERR",
    "FUNCTION_NOT_FOUND_ERR",
    "GENERIC_ERR",
]

# Library convention: emit nothing unless the application configures logging
logging.getLogger(__name__).addHandler(logging.NullHandler())
