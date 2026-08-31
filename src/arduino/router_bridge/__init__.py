# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import logging

from .bridge import (
    Bridge,
    call,
    notify,
    provide,
    shutdown,
)
from .connection import (
    DEFAULT_ADDRESS,
    BridgeConnection,
)

__all__ = [
    "Bridge",
    "notify",
    "call",
    "provide",
    "shutdown",
    "BridgeConnection",
    "DEFAULT_ADDRESS",
]

# Library convention: emit nothing unless the application configures logging
logging.getLogger(__name__).addHandler(logging.NullHandler())
