"""
test_type4_live.py

End-to-end check of the Type 4 live protocol, with a fake plugin: a real
server on a local port, the simulated world from test_type4.py producing the
OBS lines, and the ACT lines the server returns driving the world. Also checks
that an old plugin (no live=1) gets a normal recording instead of a dead end.

    py test_type4_live.py
"""

import socket
import struct
import threading
import time

import server as srv
from recording import Recording
from test_type4 import World, OBS_EVERY
from type4_policy import Action

PORT = 27911


def recv_line(s):
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            break
        buf += c
    return buf.decode()


def request(extra):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    name = b"d1_town_01"
    s.sendall(("INTERLOPE " + extra).strip().encode() + b"\n")
    s.sendall(struct.pack("<I3f3f3f", len(name), -512, -512, 0, 512, 512, 256, 0, 0, 64) + name)
    line = recv_line(s)
    s.close()
    return line


def parse_pull(line):
    head, commit = line.strip().split(" ", 1)
    return head.split(":")[0], int(head.split(":")[1]), commit


def read_act(line):
    parts = dict(p.split("=", 1) for p in line.split()[1:])
    a = Action(buttons=int(parts["b"]), yaw=float(parts["yaw"]), pitch=float(parts["pitch"]), rate=float(parts["rate"]))
    a.cmd = parts.get("cmd")
    return a


def main():
    server = srv.InterloperServer(port=PORT)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.5)

    # 1. live session
    line = request("4 game=hl2 seconds=6 seed=2 live=1")
    kind, port, commit = parse_pull(line)
    assert kind == "s.interlope.live", line
    w = World(5)
    live = socket.create_connection(("127.0.0.1", port), timeout=10)
    got_end, acts, cmds = False, 0, 0
    for _ in range(2000):
        live.sendall((w.obs_line() + "\n").encode())
        reply = recv_line(live)
        if reply.startswith("END"):
            got_end = True
            break
        assert reply.startswith("ACT "), reply
        a = read_act(reply)
        acts += 1
        cmds += 1 if a.cmd else 0
        w.run_ticks(a, OBS_EVERY)
    live.close()
    assert got_end, "server never ended the session"
    assert w.moved_ticks > 100, "player did not move"
    assert len(w.visited) >= 3
    print(f"live session ok: {acts} actions, ended by server, visited {len(w.visited)} cells, kills {w.kills}")
    time.sleep(0.3)
    assert any("observations" in s for s in server.live_log), server.live_log

    # 2. old plugin asks for type 4 without live=1 -> normal recording (type 3)
    line = request("4")
    kind, port, commit = parse_pull(line)
    assert kind == "s.interlope.pull", line
    data = socket.create_connection(("127.0.0.1", port), timeout=10)
    blob = b""
    while True:
        c = data.recv(65536)
        if not c:
            break
        blob += c
    rec = Recording.deserialize(blob)
    assert rec.demo_type == 3, rec.demo_type
    print("fallback for old plugin ok (served type 3)")

    # 3. other types unaffected by live=1
    line = request("1 live=1")
    assert parse_pull(line)[0] == "s.interlope.pull"
    # 4. plugin vanishes mid-session -> server must not crash
    kind, port, _ = parse_pull(request("4 game=portal seconds=30 live=1"))
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"OBS gt=0 x=0 y=0 z=0 yaw=0 pitch=0 hp=100\n")
    recv_line(s)
    s.close()
    time.sleep(0.3)
    print("dropped plugin handled; all live protocol checks passed")


if __name__ == "__main__":
    main()
