# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Host-only mapping for the two multicast destinations in this lab."""

APP_TO_RADIO = {
    ("239.2.3.1", 6969): ("225.0.0.201", 6969),
    ("224.10.10.1", 17012): ("225.0.0.202", 17012),
}
RADIO_TO_APP = {radio: app for app, radio in APP_TO_RADIO.items()}


def to_radio(destination: tuple[str, int], payload: bytes, limit: int = 558) -> tuple[str, int, bytes]:
    """limit: the link's largest UDP payload (558 on the USB network adapter, 556 on PPP)."""
    if destination not in APP_TO_RADIO or not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
        raise ValueError("unsupported destination or payload outside the link's UDP limit")
    address, port = APP_TO_RADIO[destination]
    return address, port, payload


def to_app(destination: tuple[str, int], payload: bytes) -> tuple[str, int, bytes]:
    if destination not in RADIO_TO_APP or not isinstance(payload, bytes) or not 0 < len(payload) <= 558:
        raise ValueError("unsupported destination or payload outside USB UDP limit")
    address, port = RADIO_TO_APP[destination]
    return address, port, payload
