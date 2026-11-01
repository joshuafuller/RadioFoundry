# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""unix:///absolute/path.sock for pyserial: the socket:// class, over an AF_UNIX socket.

The HIL endpoint (parts/hil_endpoint.py) listens on such a socket in a 0700 folder. Reads and the
20 ms timeout work as for socket://, because this reuses pyserial's own class.
"""

import socket
import urllib.parse

from serial.serialutil import SerialException
from serial.urlhandler import protocol_socket


class Serial(protocol_socket.Serial):
    def from_url(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "unix" or parts.netloc or parts.query or not parts.path.startswith("/"):
            raise SerialException(f'expected "unix:///absolute/path.sock", not {url!r}')
        return urllib.parse.unquote(parts.path)

    def open(self):
        if self._port is None:
            raise SerialException("Port must be configured before it can be used.")
        if self.is_open:
            raise SerialException("Port is already open.")
        self.logger = None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(protocol_socket.POLL_TIMEOUT)
            sock.connect(self.from_url(self.portstr))
        except Exception as error:
            sock.close()
            raise SerialException(f"Could not open port {self.portstr}: {error}")
        sock.setblocking(False)
        self._socket = sock
        self.is_open = True
        self.reset_input_buffer()
        self.reset_output_buffer()
