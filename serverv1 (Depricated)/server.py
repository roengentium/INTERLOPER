"""
server.py

Core Interloper server. Speaks a small text+binary protocol over raw TCP,
deliberately avoiding anything (JSON, protobuf, TLS) that would need extra
libraries on an old-era Source SDK client.

Protocol
--------

1. Client connects to the command port and sends a text line:

       INTERLOPE\n

   followed immediately (no newline needed) by a fixed-size request block:

       I     name_len
       3f    map bounds mins (x, y, z)
       3f    map bounds maxs (x, y, z)
       3f    player origin (x, y, z) at the moment INTERLOPE was run
       *     map_name (name_len bytes, utf-8)

   If the first line isn't exactly "INTERLOPE", the server replies with:

       Unknown Command: <what you sent>\n

   and closes the connection (matches the ARG's own console flavor text).

2. On a valid request, the server replies with a text line:

       s.interlope.pull:<port> <commit_id>\n

   and opens a one-shot listener on <port>.

3. The client connects to that port and receives the raw serialized
   Recording (see recording.py for the exact binary layout), then the
   server closes the connection. That's the whole protocol - no framing
   beyond what's already inside the Recording's own header.

Run:

    python server.py [--host 127.0.0.1] [--port 27015] [--debug]

Debug mode (--debug, or DEBUG=1 in the environment) switches on verbose
logging of every protocol step: raw request bytes received, parsed bounds,
which type got picked and why (fresh generation vs repeat-from-repo), exact
spawn points and tick counts, pull-socket lifecycle, and per-client errors
with tracebacks instead of one-line messages. Normal (non-debug) mode stays
quiet and only logs one line per request, which is what you want once this
is actually working and you're just watching it run.
"""

import argparse
import logging
import math
import os
import socket
import struct
import threading
import traceback

from generators.type1 import generate_type1
from generators.type2 import MapBounds, generate_type2
from generators.type3 import generate_type3
from recording import Recording
from repo import Repo

DEFAULT_HOST = "127.0.0.1"
DEFAULT_COMMAND_PORT = 27015
PULL_PORT_RANGE_START = 27100
PULL_ACCEPT_TIMEOUT_SECONDS = 30

REQUEST_FIXED_FMT = "<I3f3f3f"
REQUEST_FIXED_SIZE = struct.calcsize(REQUEST_FIXED_FMT)

log = logging.getLogger("interloper")


def configure_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log.setLevel(level)


class InterloperServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_COMMAND_PORT, debug: bool = False):
        self.host = host
        self.port = port
        self.debug = debug
        self.repo = Repo()
        self._next_pull_port = PULL_PORT_RANGE_START
        self._pull_port_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(8)
        log.info("listening on %s:%d (debug=%s)", self.host, self.port, self.debug)

        try:
            while True:
                conn, addr = sock.accept()
                log.debug("accepted connection from %s:%d", addr[0], addr[1])
                threading.Thread(
                    target=self._handle_client, args=(conn, addr), daemon=True
                ).start()
        except KeyboardInterrupt:
            log.info("shutting down")
        finally:
            sock.close()

    # -- command handling ---------------------------------------------

    def _handle_client(self, conn: socket.socket, addr):
        try:
            self._handle_command(conn, addr)
        except (ConnectionError, struct.error, ValueError) as exc:
            if self.debug:
                log.debug("client %s:%d error, full traceback:\n%s",
                           addr[0], addr[1], traceback.format_exc())
            else:
                log.warning("client %s:%d error: %s", addr[0], addr[1], exc)
        finally:
            conn.close()

    def _handle_command(self, conn: socket.socket, addr):
        line = self._read_line(conn)
        command = line.strip()
        log.debug("%s:%d sent command: %r", addr[0], addr[1], command)

        if command != b"INTERLOPE":
            log.debug("%s:%d unknown command, rejecting", addr[0], addr[1])
            conn.sendall(b"Unknown Command: " + command + b"\n")
            return

        req = self._recv_exact(conn, REQUEST_FIXED_SIZE)
        name_len, minx, miny, minz, maxx, maxy, maxz, ox, oy, oz = struct.unpack(REQUEST_FIXED_FMT, req)
        map_name_raw = self._recv_exact(conn, name_len) if name_len else b""
        map_name = map_name_raw.decode("utf-8", "replace")

        bounds = MapBounds(mins=(minx, miny, minz), maxs=(maxx, maxy, maxz))
        # A NaN/inf origin from a misbehaving client must not reach a generator.
        origin = (ox, oy, oz) if all(math.isfinite(v) for v in (ox, oy, oz)) else None
        log.debug(
            "%s:%d request block: map=%r name_len=%d mins=%s maxs=%s origin=%s",
            addr[0], addr[1], map_name, name_len, bounds.mins, bounds.maxs, origin,
        )

        commit_id, recording = self.repo.pull_random_existing()
        if recording is not None:
            log.debug("%s:%d repo repeat hit -> reserving existing commit %s", addr[0], addr[1], commit_id)
        else:
            demo_type = self.repo.pick_type()
            log.debug("%s:%d generating fresh demo, picked type %d", addr[0], addr[1], demo_type)
            recording = self._generate(demo_type, bounds, origin)
            commit_id = self.repo.commit(recording, commit_id=recording.commit_number)
            log.debug(
                "%s:%d generated recording: type=%d commit=%s ticks=%d events=%s",
                addr[0], addr[1], recording.demo_type, commit_id,
                len(recording.ticks), recording.events,
            )
            if recording.ticks:
                log.debug(
                    "%s:%d spawn origin=%s first_angles=%s",
                    addr[0], addr[1], recording.ticks[0].origin, recording.ticks[0].angles,
                )

        # Bind + listen BEFORE replying, on this thread. If the socket were
        # bound inside the pull thread, a fast client could read the reply and
        # connect to the pull port before the listener existed (refused).
        pull_port, pull_sock = self._open_pull_listener()
        log.debug("%s:%d reserved pull port %d", addr[0], addr[1], pull_port)
        threading.Thread(
            target=self._serve_pull, args=(pull_sock, pull_port, recording), daemon=True
        ).start()

        log.info(
            "%s:%d map='%s' -> type %d, commit %s, pull port %d",
            addr[0], addr[1], map_name, recording.demo_type, commit_id, pull_port,
        )

        response = f"s.interlope.pull:{pull_port} {commit_id}\n".encode("ascii")
        conn.sendall(response)
        log.debug("%s:%d sent response: %r", addr[0], addr[1], response)

    def _generate(self, demo_type: int, bounds: MapBounds, origin=None) -> Recording:
        # Types 1, 2 and 3 are implemented so far; everything else falls back to
        # Type 2 until its own generator lands. The fallback is logged (even
        # outside --debug) specifically so "picked type 3" vs "generated
        # type=2" doesn't look like a silent bug when you're watching logs -
        # it's very much on purpose right now, but only until more
        # generators exist. Swap this dispatch table out as they land.
        # origin is the player's real position; only generators that care
        # about it (type 1 twists the camera where the player stands) use it.
        generators = {
            1: lambda: generate_type1(bounds, origin=origin),
            2: lambda: generate_type2(bounds),
            3: lambda: generate_type3(bounds, origin=origin),
        }
        generator = generators.get(demo_type)
        if generator is None:
            log.info("type %d has no generator yet, falling back to type 2", demo_type)
            generator = generators[2]
        return generator()

    def _open_pull_listener(self):
        """Reserve the next free pull port and return (port, listening socket).
        Skips ports that are already in use instead of failing the request."""
        with self._pull_port_lock:
            for _ in range(200):
                port = self._next_pull_port
                self._next_pull_port += 1
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((self.host, port))
                    sock.listen(1)
                except OSError:
                    sock.close()
                    continue
                return port, sock
        raise ConnectionError("no free pull port available")

    def _serve_pull(self, sock: socket.socket, port: int, recording: Recording):
        try:
            sock.settimeout(PULL_ACCEPT_TIMEOUT_SECONDS)
            log.debug("pull port %d waiting for client", port)
            conn, pull_addr = sock.accept()
            log.debug("pull port %d accepted from %s:%d", port, pull_addr[0], pull_addr[1])
            try:
                payload = recording.serialize()
                conn.sendall(payload)
                log.debug("pull port %d sent %d bytes, closing", port, len(payload))
            finally:
                conn.close()
        except socket.timeout:
            log.warning("pull port %d timed out waiting for client", port)
        finally:
            sock.close()

    # -- small socket helpers ------------------------------------------

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes:
        buf = bytearray()
        while True:
            chunk = conn.recv(1)
            if not chunk:
                break
            buf += chunk
            if chunk == b"\n":
                break
        return bytes(buf)

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes:
        if n == 0:
            return b""
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed before expected data arrived")
            buf += chunk
        return bytes(buf)


def main():
    parser = argparse.ArgumentParser(description="Interloper core server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_COMMAND_PORT)
    parser.add_argument(
        "--debug", action="store_true",
        default=os.environ.get("DEBUG", "") not in ("", "0"),
        help="verbose per-step logging (also enabled by DEBUG=1 in the environment)",
    )
    args = parser.parse_args()

    configure_logging(args.debug)
    InterloperServer(host=args.host, port=args.port, debug=args.debug).start()


if __name__ == "__main__":
    main()
