# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Host-only mapping for the two multicast destinations in this lab."""

from datetime import datetime
import xml.etree.ElementTree as ET

APP_TO_RADIO = {
    ("239.2.3.1", 6969): ("225.0.0.201", 6969),
    ("224.10.10.1", 17012): ("225.0.0.202", 17012),
}
RADIO_TO_APP = {radio: app for app, radio in APP_TO_RADIO.items()}


def to_radio(destination: tuple[str, int], payload: bytes, limit: int = 558) -> tuple[str, int, bytes]:
    """limit: the link's largest UDP payload (558 on the USB network adapter, 556 on PPP)."""
    if destination not in APP_TO_RADIO:
        raise ValueError("unsupported destination")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("payload must be nonempty bytes")
    if len(payload) > limit:
        raise ValueError(f"event is {len(payload)} bytes; link UDP limit is {limit}")
    address, port = APP_TO_RADIO[destination]
    return address, port, payload


def to_app(destination: tuple[str, int], payload: bytes) -> tuple[str, int, bytes]:
    if destination not in RADIO_TO_APP or not isinstance(payload, bytes) or not 0 < len(payload) <= 558:
        raise ValueError("unsupported destination or payload outside USB UDP limit")
    address, port = RADIO_TO_APP[destination]
    return address, port, payload


class Pending:
    """Host-side unsent CoT events. Only newer positions for the same UID coalesce."""

    def __init__(self, limit=556):
        self.limit = limit
        self.events = []

    def put(self, destination, payload):
        mapped = to_radio(destination, payload, self.limit)
        try:
            event = ET.fromstring(payload)
            uid = event.attrib["uid"]
            stamp = datetime.fromisoformat(event.attrib["time"].replace("Z", "+00:00"))
            if event.tag != "event" or not uid or stamp.tzinfo is None:
                raise ValueError("event needs a UID and timezone")
        except (ET.ParseError, KeyError, ValueError) as error:
            raise ValueError(f"invalid CoT event: {error}") from error
        position = event.attrib.get("type", "").startswith("a-") and event.find("point") is not None
        key = (destination, uid) if position else None
        if key is not None:
            for index, (old_key, old_stamp, _) in enumerate(self.events):
                if key == old_key:
                    if stamp > old_stamp:
                        self.events[index] = (key, stamp, mapped)
                    return
        self.events.append((key, stamp, mapped))

    def pop(self):
        return self.events.pop(0)[2]
