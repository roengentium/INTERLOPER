"""
test_client.py

Minimal reference client that speaks the protocol exactly as a real game
client mod would, so you can sanity-check the server without building the
MM:S side yet. Prints out the decoded Recording it gets back.

Run the server in one terminal, then:

    python test_client.py                    (random pick)
    python test_client.py 1                  (ask for type 1)
    python test_client.py 1 yaw_speed=90 pos=void calm=0
    python test_client.py --port 27500 7
"""

import sys

import socket
import struct

from recording import Recording

HOST = "127.0.0.1"
COMMAND_PORT = 27600

# Pretend map bounds, e.g. a small HL2-scale room
FAKE_MINS = (-512.0, -512.0, 0.0)
FAKE_MAXS = (512.0, 512.0, 256.0)
FAKE_MAP_NAME = "d1_town_01"
FAKE_ORIGIN = (0.0, 0.0, 64.0)  # pretend the player is standing near the room's center


def request_demo(extra: str = "") -> Recording:
    name_bytes = FAKE_MAP_NAME.encode("utf-8")
    request = struct.pack(
        "<I3f3f3f",
        len(name_bytes),
        *FAKE_MINS,
        *FAKE_MAXS,
        *FAKE_ORIGIN,
    ) + name_bytes

    with socket.create_connection((HOST, COMMAND_PORT)) as conn:
        conn.sendall(("INTERLOPE " + extra).strip().encode() + b"\n")
        conn.sendall(request)
        response = _read_line(conn)

    print(f"server said: {response.decode('ascii').strip()}")

    addr_part, commit_id = response.decode("ascii").split()
    _, port_str = addr_part.split(":")
    pull_port = int(port_str)

    with socket.create_connection((HOST, pull_port)) as pull_conn:
        data = _recv_all(pull_conn)

    recording = Recording.deserialize(data)
    print(f"commit_id from response: {commit_id}")
    print(f"decoded commit_number:   {recording.commit_number}")
    print(f"demo_type: {recording.demo_type}")
    print(f"tick_count: {len(recording.ticks)}")
    print(f"first tick origin: {recording.ticks[0].origin}")
    print(f"first tick angles: {recording.ticks[0].angles}")
    print(f"last tick origin:  {recording.ticks[-1].origin}")
    print(f"events: {recording.events}")

    return recording


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


def _recv_all(conn: socket.socket) -> bytes:
    chunks = []
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--port" in argv:
        i = argv.index("--port")
        COMMAND_PORT = int(argv[i + 1])
        del argv[i:i + 2]
    request_demo(" ".join(argv))
