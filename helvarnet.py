"""HelvarNet transport - shared by the mapper and the controller.

The protocol is plain ASCII over TCP port 50000. Commands look like

    >V:1,C:14,L:100,F:0,@1.1.2.15#     full level on one luminaire
    >V:1,C:13,G:5,L:0,F:0#             group 5 off

and replies come back as '?...=value#' for an answer or '!...=code#' for an
error. Fade times are in hundredths of a second.
"""

import socket
import threading
import time

# Command numbers follow the HelvarNet overview document. They are passed in
# from each tool's config so they can be corrected without touching this file.
DEFAULT_COMMANDS = {
    "direct_level_device": 14,
    "direct_level_group": 13,
    "query_device_type": 103,
    "query_device_description": 105,
}


class HelvarClient:
    """One persistent TCP connection to the router, serialised by a lock.

    Routers accept a limited number of concurrent connections, so we keep a
    single socket open and reconnect on failure rather than dialling per
    command.
    """

    def __init__(self, ip, port=50000, command_timeout=1.0, commands=None):
        self.ip = ip
        self.port = port
        self.command_timeout = command_timeout
        self.commands = dict(DEFAULT_COMMANDS)
        if commands:
            self.commands.update(commands)
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()
        self.last_error = None

    # -- connection -------------------------------------------------------

    def _connect(self):
        self._close()
        sock = socket.create_connection((self.ip, self.port), timeout=3.0)
        sock.settimeout(self.command_timeout)
        self._sock = sock
        self._buf = b""

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""

    def close(self):
        with self._lock:
            self._close()

    def _read_reply(self, timeout):
        """Read until a terminated message arrives, skipping router pushes.

        Replies end with '#'. A router with push messages enabled also sends
        unsolicited '>' commands down the same socket; those are not answers
        to anything we asked, so they get discarded.
        """
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(b"#")
            if idx != -1:
                msg = self._buf[: idx + 1]
                self._buf = self._buf[idx + 1:]
                text = msg.decode("ascii", "replace").strip()
                if text.startswith(">"):
                    continue  # unsolicited push, not our reply
                return text

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("router closed the connection")
            self._buf += chunk

    # -- sending ----------------------------------------------------------

    def send(self, command, expect_reply=False, timeout=None):
        """Send a raw HelvarNet string. Returns the reply text, or None."""
        if timeout is None:
            timeout = self.command_timeout
        if not command.endswith("#"):
            command += "#"

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(command.encode("ascii"))
                    if not expect_reply:
                        self.last_error = None
                        return None
                    reply = self._read_reply(timeout)
                    self.last_error = None
                    return reply
                except (OSError, ConnectionError) as exc:
                    self._close()
                    if attempt == 2:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        raise
        return None

    def direct_level_device(self, address, level, fade=0):
        self.send(">V:1,C:%d,L:%d,F:%d,%s" % (
            self.commands["direct_level_device"], level, fade, address))

    def direct_level_group(self, group, level, fade=0):
        self.send(">V:1,C:%d,G:%s,L:%d,F:%d" % (
            self.commands["direct_level_group"], group, level, fade))

    def query_device(self, address, command_number, timeout):
        return self.send(">V:1,C:%d,%s" % (command_number, address),
                         expect_reply=True, timeout=timeout)


def parse_reply(reply):
    """Split a HelvarNet reply into (ok, value).

    '?' prefixes an answer, '!' an error. The payload follows '='.
    """
    if not reply:
        return False, None
    ok = reply.startswith("?")
    value = None
    if "=" in reply:
        value = reply.split("=", 1)[1].rstrip("#").strip()
    return ok, value


def address_sort_key(address):
    """Sort '@1.1.2.15' numerically rather than as text."""
    try:
        return tuple(int(part) for part in address.lstrip("@").split("."))
    except ValueError:
        return (0, 0, 0, 0)
