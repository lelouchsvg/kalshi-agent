"""A small WebSocket client (RFC 6455) built on the standard library.

The agent only needs to read JSON text messages from a couple of public feeds, so a
few dozen lines here avoid adding another library to install on the Mac.
Partial frames survive read timeouts: incomplete bytes stay buffered until the rest
arrives, so callers can use short timeouts to check for shutdown.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 16 * 1024 * 1024

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    pass


def _ssl_context() -> ssl.SSLContext:
    try:  # certifi ships with requests; the private Python may not see macOS's keychain
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()


class WebSocket:
    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 connect_timeout: float = 10, read_timeout: float = 5):
        self.url = url
        self.headers = headers or {}
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.sock: socket.socket | None = None
        self._buf = bytearray()
        self._frags: list[bytes] = []
        self._frag_op = OP_TEXT

    # -- connection ------------------------------------------------------------
    def connect(self) -> "WebSocket":
        u = urlsplit(self.url)
        if u.scheme not in ("ws", "wss"):
            raise WebSocketError(f"not a websocket url: {self.url}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        sock = socket.create_connection((u.hostname, port), timeout=self.connect_timeout)
        if u.scheme == "wss":
            sock = _ssl_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "User-Agent: kalshi-agent"]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WebSocketError("connection closed during handshake")
            resp += chunk
            if len(resp) > 65536:
                sock.close()
                raise WebSocketError("handshake response too large")
        head, _, rest = bytes(resp).partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in f"{status_line} ":
            sock.close()
            raise WebSocketError(f"handshake refused: {status_line}")
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in header_lines)}
        expect = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if hdrs.get("sec-websocket-accept") != expect:
            sock.close()
            raise WebSocketError("handshake failed: bad Sec-WebSocket-Accept")
        sock.settimeout(self.read_timeout)
        self.sock = sock
        self._buf = bytearray(rest)
        return self

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send(OP_CLOSE, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    # -- sending ---------------------------------------------------------------
    def _send(self, op: int, payload: bytes) -> None:
        if self.sock is None:
            raise WebSocketClosed("not connected")
        n = len(payload)
        head = bytearray([0x80 | op])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text: str) -> None:
        self._send(OP_TEXT, text.encode())

    def ping(self, data: bytes = b"") -> None:
        self._send(OP_PING, data)

    # -- receiving -------------------------------------------------------------
    def _parse_frame(self) -> tuple[bool, int, bytes] | None:
        b = self._buf
        if len(b) < 2:
            return None
        fin, op = bool(b[0] & 0x80), b[0] & 0x0F
        masked, n, i = bool(b[1] & 0x80), b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        if n > MAX_MESSAGE:
            raise WebSocketError(f"frame too large ({n} bytes)")
        mask = b""
        if masked:
            if len(b) < i + 4:
                return None
            mask, i = bytes(b[i:i + 4]), i + 4
        if len(b) < i + n:
            return None
        payload = bytes(b[i:i + n])
        if masked:
            payload = bytes(x ^ mask[j % 4] for j, x in enumerate(payload))
        del b[:i + n]
        return fin, op, payload

    def recv(self) -> str:
        """Return the next text message. Raises socket.timeout if none arrives within
        read_timeout (buffered partial data is kept), WebSocketClosed when the server closes."""
        if self.sock is None:
            raise WebSocketClosed("not connected")
        while True:
            frame = self._parse_frame()
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WebSocketClosed("connection closed by server")
                self._buf += chunk
                continue
            fin, op, payload = frame
            if op == OP_PING:
                self._send(OP_PONG, payload)
            elif op == OP_PONG:
                pass
            elif op == OP_CLOSE:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                try:
                    self._send(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                raise WebSocketClosed(f"server closed the connection (code {code})")
            elif op in (OP_TEXT, OP_BIN, OP_CONT):
                if op != OP_CONT:
                    self._frags, self._frag_op = [], op
                self._frags.append(payload)
                if sum(map(len, self._frags)) > MAX_MESSAGE:
                    raise WebSocketError("message too large")
                if fin:
                    data, self._frags = b"".join(self._frags), []
                    return data.decode("utf-8", errors="replace")
            else:
                raise WebSocketError(f"unknown opcode {op}")
