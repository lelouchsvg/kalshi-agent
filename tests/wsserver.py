"""A tiny WebSocket server for tests (plain ws:// on localhost)."""
import base64
import hashlib
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(payload: bytes, op: int = 1, fin: bool = True) -> bytes:
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def read_client_frame(conn: socket.socket) -> tuple[int, bytes]:
    def exact(n):
        b = b""
        while len(b) < n:
            chunk = conn.recv(n - len(b))
            if not chunk:
                raise ConnectionError
            b += chunk
        return b
    b0, b1 = exact(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exact(8))[0]
    assert b1 & 0x80, "client frames must be masked"
    mask = exact(4)
    data = bytes(x ^ mask[i % 4] for i, x in enumerate(exact(n)))
    return b0 & 0x0F, data


class WSServer:
    """Accepts one connection at a time and runs `script(conn, request_headers)`."""

    def __init__(self, script):
        self.script = script
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/feed"
        self.requests = []
        self.received = []
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                req = b""
                while b"\r\n\r\n" not in req:
                    req += conn.recv(4096)
                lines = req.decode().split("\r\n")
                hdrs = {k.lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if l)}
                self.requests.append(hdrs)
                accept = base64.b64encode(hashlib.sha1((hdrs["sec-websocket-key"] + GUID).encode()).digest()).decode()
                conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                self.script(conn, self)
            except (ConnectionError, OSError):
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()
