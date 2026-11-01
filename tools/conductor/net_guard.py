# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""All conductor TCP/UDP listeners must bind a numeric loopback address."""

import ipaddress


def loopback_address(address):
    try:
        ip = ipaddress.ip_address(address[0])
    except ValueError as error:
        raise ValueError("WORLD_NON_LOOPBACK_BIND") from error
    if not ip.is_loopback:
        raise ValueError("WORLD_NON_LOOPBACK_BIND")
    return address


def bind(listener, address):
    listener.bind(loopback_address(address))
