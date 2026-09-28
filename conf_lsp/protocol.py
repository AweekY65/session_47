"""LSP-style JSON-RPC message framing over stdin/stdout.

A frame is a small ASCII header ending with a blank line followed by a
UTF-8 JSON body::

    Content-Length: 54\\r\\n
    \\r\\n
    {"jsonrpc":"2.0","id":1,"method":"shutdown"}

Both CRLF and bare LF line endings are accepted on read; writes always use
CRLF as the spec demands.
"""

from __future__ import annotations

import json


class FrameReader:
    def __init__(self, stream):
        self.stream = stream
        self._buf = b""

    def _read_line(self):
        """Read one header line, tolerating LF or CRLF, returning without it."""
        # Consume buffered data first so multiple frames in one OS read work.
        line = b""
        while True:
            if self._buf:
                byte = self._buf[:1]
                self._buf = self._buf[1:]
            else:
                byte = self.stream.read(1)
            if not byte:
                return None if not line else line
            line += byte
            if line.endswith(b"\n"):
                return line[:-2] if line.endswith(b"\r\n") else line[:-1]

    def read_frame(self):
        """Read one complete frame and return the parsed JSON payload.

        Returns ``None`` at clean end of input. Raises
        :class:`ProtocolError` on malformed framing.
        """
        content_length = None
        while True:
            line = self._read_line()
            if line is None:
                return None
            if line == b"":
                break  # end of headers
            if b":" not in line:
                raise ProtocolError("malformed header line: %r" % line)
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                content_length = int(value.strip())
        if content_length is None:
            raise ProtocolError("missing Content-Length header")

        body = self._read_exact(content_length)
        if body is None:
            raise ProtocolError("connection closed mid-body")
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError("invalid JSON body: %s" % exc) from exc

    def _read_exact(self, n: int):
        chunks = []
        remaining = n
        if self._buf:
            take = self._buf[:remaining]
            chunks.append(take)
            remaining -= len(take)
            self._buf = self._buf[len(take):]
        while remaining > 0:
            chunk = self.stream.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class FrameWriter:
    def __init__(self, stream):
        self.stream = stream

    def write_frame(self, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.stream.write(b"Content-Length: %d\r\n\r\n" % len(body))
        self.stream.write(body)
        self.stream.flush()


class ProtocolError(Exception):
    pass
