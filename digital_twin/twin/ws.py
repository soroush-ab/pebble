"""Minimal RFC 6455 WebSocket server over http.server. Stdlib only.

Scope: LAN development use for the Pebble digital twin. Supports text, binary,
ping/pong, close and continuation frames. No extensions, no permessage-deflate.
"""

import base64
import hashlib
import json
import socket
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WSClosed(Exception):
    """Raised by WSConn.recv when the peer has gone away."""


class WSConn:
    """One upgraded connection. Sends are serialised; recv is single-reader."""

    def __init__(self, rfile, wfile, addr):
        self.rfile = rfile
        self.wfile = wfile
        self.addr = addr
        self._send_lock = threading.Lock()
        self._closed = False

    # -- sending ---------------------------------------------------------

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(n)
        elif n < 65536:
            head.append(126)
            head += struct.pack(">H", n)
        else:
            head.append(127)
            head += struct.pack(">Q", n)
        return bytes(head) + payload

    def _send_raw(self, opcode: int, payload: bytes) -> bool:
        if self._closed:
            return False
        with self._send_lock:
            try:
                self.wfile.write(self._frame(opcode, payload))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                self._closed = True
                return False

    def send_text(self, text: str) -> bool:
        return self._send_raw(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj) -> bool:
        return self.send_text(json.dumps(obj, separators=(",", ":")))

    def send_bytes(self, data: bytes) -> bool:
        return self._send_raw(OP_BIN, data)

    def close(self, code: int = 1000) -> None:
        if not self._closed:
            self._send_raw(OP_CLOSE, struct.pack(">H", code))
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- receiving -------------------------------------------------------

    def _read_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                raise WSClosed()
            buf += chunk
        return buf

    def recv(self):
        """Block for one complete message. Returns str, bytes, or raises WSClosed.

        Control frames are handled transparently and never returned.
        """
        frags = bytearray()
        frag_op = None
        while True:
            try:
                b0, b1 = self._read_exactly(2)
            except (OSError, ValueError):
                raise WSClosed()

            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F

            if length == 126:
                (length,) = struct.unpack(">H", self._read_exactly(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exactly(8))

            mask = self._read_exactly(4) if masked else None
            payload = self._read_exactly(length) if length else b""
            if mask:
                payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))

            if opcode == OP_CLOSE:
                self._closed = True
                raise WSClosed()
            if opcode == OP_PING:
                self._send_raw(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode == OP_CONT:
                if frag_op is None:          # continuation with nothing to continue
                    raise WSClosed()
                frags += payload
            else:
                frag_op = opcode
                frags = bytearray(payload)

            if fin:
                data = bytes(frags)
                return data.decode("utf-8", "replace") if frag_op == OP_TEXT else data

    def recv_json(self):
        msg = self.recv()
        if isinstance(msg, bytes):
            return None
        try:
            return json.loads(msg)
        except json.JSONDecodeError:
            return None


def upgrade(handler) -> "WSConn | None":
    """Complete the handshake on a BaseHTTPRequestHandler. None if not a WS request."""
    if handler.headers.get("Upgrade", "").lower() != "websocket":
        return None
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        return None

    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
    )
    handler.wfile.write(resp.encode("ascii"))
    handler.wfile.flush()

    try:                                     # latency matters on the 50 Hz stream
        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    handler.close_connection = True          # stop http.server parsing further requests
    return WSConn(handler.rfile, handler.wfile, handler.client_address)


# ----------------------------------------------------------- client side

def connect(url: str, timeout: float = 10.0) -> WSConn:
    """Minimal client. `url` is ws://host:port/path. Raises OSError if refused."""
    import os as _os
    import urllib.parse

    u = urllib.parse.urlparse(url)
    if u.scheme != "ws":
        raise ValueError("only ws:// is supported")
    port = u.port or 80
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    key = base64.b64encode(_os.urandom(16)).decode()
    path = u.path or "/"
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())

    rfile = sock.makefile("rb")
    status = rfile.readline()
    if b"101" not in status:
        raise ConnectionError(f"upgrade refused: {status!r}")
    while rfile.readline() not in (b"\r\n", b"\n", b""):
        pass
    # the timeout was for the handshake only; a brain may sit idle for minutes
    sock.settimeout(None)

    conn = _ClientConn(rfile, sock.makefile("wb"), (u.hostname, port))
    conn._sock = sock
    return conn


class _ClientConn(WSConn):
    """Same as WSConn but masks outgoing frames, as RFC 6455 requires of clients."""

    def _frame(self, opcode, payload):
        import os as _os
        mask = _os.urandom(4)
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        masked = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        return bytes(head) + mask + masked
