# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

import logging

from .bridge import Bridge
from .transport import DEFAULT_ADDRESS

__all__ = [
    "Bridge",
    "DEFAULT_ADDRESS",
]

# Library convention: emit nothing unless the application configures logging
logging.getLogger(__name__).addHandler(logging.NullHandler())
