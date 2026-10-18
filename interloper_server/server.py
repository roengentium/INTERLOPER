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

Choosing which demo
-------------------

Three ways, all stackable (per-request beats server-wide):

  * In the game console:      interlope 3            (type 3)
                              interlope 1 yaw_speed=90 calm=0 pos=void
                              interlope commit=13-70-18   (replay a stored commit)
    The line sent to the server is "INTERLOPE <type> key=value ...".
  * Server-wide at start-up:  python server.py --force-type 1 --no-repeat
  * Live, by typing in the server window:   type 7 / type random / repeat off
    / opts ticks=300 / status / help

Debug mode (--debug, or DEBUG=1 in the environment) switches on verbose
logging of every protocol step: raw request bytes received, parsed bounds,
which type got picked and why (fresh generation vs repeat-from-repo), exact
spawn points and tick counts, pull-socket lifecycle, and per-client errors
with tracebacks instead of one-line messages. Normal (non-debug) mode stays
quiet and only logs one line per request, which is what you want once this
is actually working and you're just watching it run.
"""

import argparse
import inspect
import json
import logging
import math
import os
import socket
import struct
import threading
import sys
import traceback

from generators.type1 import generate_type1
from generators.type2 import MapBounds, generate_type2
from generators.type3 import generate_type3
from generators.type6 import generate_type6
from generators.type7 import generate_type7
from ollama_brain import OllamaBrain
from recording import Recording
from repo import Repo
from type4_policy import LetsPlayer, parse_obs

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
        self.forced_type = None        # None = weighted random
        self.repeat_chance = 0.1       # chance of re-serving a stored commit
        self.default_opts = {}         # generator options applied to every request
        self.dump_dir = None           # if set, every recording is dumped there as JSON
        self.last = None               # last Recording served (for the `last` command)
        self.ollama_model = None       # e.g. "llama3": gives Type 4 an LLM brain
        self.ollama_host = "http://127.0.0.1:11434"
        self.live_log = []             # summaries of finished Type 4 sessions
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

        parts = command.split()
        if not parts or parts[0] != b"INTERLOPE":
            log.debug("%s:%d unknown command, rejecting", addr[0], addr[1])
            conn.sendall(b"Unknown Command: " + command + b"\n")
            return
        req_opts = self._parse_opts([p.decode("utf-8", "replace") for p in parts[1:]])
        if req_opts:
            log.info("%s:%d request options: %s", addr[0], addr[1], req_opts)

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

        opts = dict(self.default_opts)
        opts.update(req_opts)

        commit_id, recording = None, None
        want_commit = opts.pop("commit", None)
        if want_commit:
            recording = self.repo.pull(str(want_commit))
            if recording is None:
                log.warning("commit %s requested but not in the repo (%d stored); generating instead",
                            want_commit, len(self.repo))
            else:
                commit_id = str(want_commit)
                log.info("replaying stored commit %s (type %d)", commit_id, recording.demo_type)

        # Which type? request > server-forced > weighted random.
        forced = opts.pop("type", None)
        if forced is None:
            forced = self.forced_type
        repeat = float(opts.pop("repeat", self.repeat_chance))
        repeat = 1.0 if repeat > 1 else max(0.0, repeat)

        if recording is None and forced is None:
            commit_id, recording = self.repo.pull_random_existing(repeat_chance=repeat)
            if recording is not None:
                log.info("repeat hit: re-serving stored commit %s (type %d). "
                         "Use --no-repeat / `repeat off` to stop this.", commit_id, recording.demo_type)

        if recording is not None:
            log.debug("%s:%d reserving existing commit %s", addr[0], addr[1], commit_id)
        else:
            demo_type = int(forced) if forced is not None else self.repo.pick_type(game=opts.get("game"))
            why = "forced" if forced is not None else "weighted random"
            live_ok = str(opts.pop("live", "0")) not in ("0", "", "false", "off")
            if demo_type == 4:
                if live_ok:
                    self._start_live(conn, addr, opts, map_name)
                    return
                log.warning("type 4 needs a live-capable plugin (this client did not send live=1); "
                            "serving type 3 instead. Rebuild the plugin from the latest update.")
                demo_type = 3
            log.debug("%s:%d generating fresh demo, type %d (%s)", addr[0], addr[1], demo_type, why)
            recording = self._generate(demo_type, bounds, origin, opts)
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

        self.last = recording
        self._maybe_dump(recording, commit_id)

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

    # -- Type 4: live sessions --------------------------------------------

    def _make_brain(self, opts):
        mode = str(opts.pop("brain", "ollama" if self.ollama_model else "off")).lower()
        model = str(opts.pop("model", self.ollama_model or "llama3"))
        host = str(opts.pop("ollama_host", self.ollama_host))
        if mode in ("off", "0", "none", "rules", "false"):
            return None
        log.info("type 4 brain: Ollama model %s at %s (falls back to rules if it does not answer)", model, host)
        return OllamaBrain(model=model, host=host)

    def _start_live(self, conn, addr, opts, map_name):
        """Type 4 is closed loop: instead of a recording, hand the plugin a live port and
        play the game with the policy, one observation at a time."""
        opts = dict(opts)
        game = opts.pop("game", None)
        seed = int(opts["seed"]) if "seed" in opts else None
        seconds = float(opts["seconds"]) if "seconds" in opts else None
        brain = self._make_brain(opts)
        commit_id = Repo.new_commit_id()
        port, sock = self._open_pull_listener()
        threading.Thread(
            target=self._serve_live, args=(sock, port, game, seed, seconds, brain, commit_id, map_name),
            daemon=True,
        ).start()
        log.info("%s:%d map='%s' game=%s -> type 4 (live), commit %s, live port %d",
                 addr[0], addr[1], map_name, game, commit_id, port)
        conn.sendall(f"s.interlope.live:{port} {commit_id}\n".encode("ascii"))

    def _serve_live(self, sock, port, game, seed, seconds, brain, commit_id, map_name):
        player = LetsPlayer(game=game, seed=seed, seconds=seconds, brain=brain)
        steps = 0
        try:
            sock.settimeout(PULL_ACCEPT_TIMEOUT_SECONDS)
            conn, peer = sock.accept()
        except socket.timeout:
            log.warning("live port %d: plugin never connected", port)
            sock.close()
            return
        sock.close()
        log.info("live %s: playing %s as profile '%s' for ~%.0fs", commit_id, map_name, player.profile.name, player.duration)
        try:
            conn.settimeout(15.0)
            buf = b""
            while True:
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise ConnectionError("plugin closed the live link")
                    buf += chunk
                line, _, buf = buf.partition(b"\n")
                if not line.startswith(b"OBS"):
                    continue
                obs = parse_obs(line.decode("utf-8", "replace"))
                act = player.step(obs)
                steps += 1
                if self.debug and steps % 10 == 1:
                    log.debug("live obs %d: pos=(%.0f,%.0f) yaw=%.0f rf=%.0f ents=%d -> %s",
                              steps, obs.x, obs.y, obs.yaw, obs.rf, len(obs.ents), act.line().strip())
                conn.sendall(act.line().encode("ascii"))
                if act.end:
                    break
        except (ConnectionError, socket.timeout, OSError) as exc:
            log.info("live %s ended: %s", commit_id, exc)
        finally:
            conn.close()
        summary = f"live {commit_id}: {steps} observations, stats {player.stats}"
        if brain is not None:
            summary += f", brain answered {brain.queries} times"
        log.info(summary)
        self.live_log.append(summary)

    def _generate(self, demo_type: int, bounds: MapBounds, origin=None, opts=None) -> Recording:
        """Dispatch to a generator. Types without a generator fall back to
        type 2 (logged loudly). opts are request/server generator options;
        each is matched to the generator's parameters by name and coerced to
        the type of that parameter's default. Unknown keys are warned about
        rather than silently ignored."""
        opts = dict(opts or {})
        generators = {
            1: generate_type1, 2: generate_type2, 3: generate_type3,
            6: generate_type6, 7: generate_type7,
        }
        fn = generators.get(demo_type)
        if fn is None:
            log.info("type %d has no generator yet, falling back to type 2", demo_type)
            demo_type, fn = 2, generators[2]

        params = inspect.signature(fn).parameters
        # Convenience aliases: ticks=N / seconds=N pin the duration.
        if "ticks" in opts and "min_ticks" in params:
            opts["min_ticks"] = opts["max_ticks"] = opts.pop("ticks")
        if "seconds" in opts and "min_seconds" in params:
            opts["min_seconds"] = opts["max_seconds"] = opts.pop("seconds")

        # `game` is global: the plugin sends the game folder name. Only
        # generators that care about it (type 3) receive it; others skip it
        # quietly instead of warning.
        game = opts.pop("game", None)
        kwargs = {}
        if game is not None and "game" in params:
            kwargs["game"] = game
        if "origin" in params:
            kwargs["origin"] = origin
        for key, raw in opts.items():
            if key not in params or key in ("bounds", "origin"):
                log.warning("type %d ignores option %r (valid: %s)", demo_type, key,
                            ", ".join(k for k in params if k not in ("bounds", "origin")))
                continue
            kwargs[key] = self._coerce(raw, params[key].default)
        log.debug("generator type %d kwargs: %s", demo_type, kwargs)
        return fn(bounds, **kwargs)

    @staticmethod
    def _coerce(raw, default):
        if not isinstance(raw, str):
            return raw
        if isinstance(default, bool):
            return raw.lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
        if default is None or isinstance(default, str):
            low = raw.lower()
            if low in ("true", "false", "on", "off"):
                return low in ("true", "on")
            for cast in (int, float):
                try:
                    return cast(raw)
                except ValueError:
                    pass
        return raw

    @staticmethod
    def _parse_opts(tokens):
        """['3', 'yaw_speed=90'] -> {'type': '3', 'yaw_speed': '90'}"""
        out = {}
        for tok in tokens:
            if "=" in tok:
                k, v = tok.split("=", 1)
                if k:
                    out[k.strip().lower()] = v.strip()
            elif tok.isdigit():
                out["type"] = tok
        return out

    def _maybe_dump(self, recording: Recording, commit_id):
        if not self.dump_dir:
            return
        try:
            os.makedirs(self.dump_dir, exist_ok=True)
            path = os.path.join(self.dump_dir, f"type{recording.demo_type}_{commit_id}.json")
            with open(path, "w") as f:
                json.dump({
                    "type": recording.demo_type, "commit": commit_id,
                    "ticks": [[*t.origin, *t.angles, int(t.buttons), *t.move] for t in recording.ticks],
                    "events": [[e.tick_index, int(e.event_type), e.param.decode("ascii", "replace")]
                               for e in recording.events],
                }, f)
            log.info("dumped recording to %s", path)
        except OSError as exc:
            log.warning("could not dump recording: %s", exc)

    # -- live server console -------------------------------------------

    def console_loop(self):
        """Type commands in the server window while it runs."""
        for raw in sys.stdin:
            line = raw.strip()
            if line:
                print(self.run_console_command(line), flush=True)

    def run_console_command(self, line: str) -> str:
        cmd, _, rest = line.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd == "type":
            if rest.lower() in ("", "random", "off", "0"):
                self.forced_type = None
                return "type: weighted random"
            if rest.isdigit():
                self.forced_type = int(rest)
                return f"type: always {self.forced_type} (repeats are skipped while a type is forced)"
            return "usage: type <1-7|random>"
        if cmd == "repeat":
            if rest.lower() in ("off", "0", "no"):
                self.repeat_chance = 0.0
            elif rest.lower() in ("on", "1", "yes"):
                self.repeat_chance = 0.1
            else:
                try:
                    self.repeat_chance = max(0.0, min(1.0, float(rest)))
                except ValueError:
                    return "usage: repeat <on|off|0.0-1.0>"
            return f"repeat chance: {self.repeat_chance}"
        if cmd == "opts":
            if rest.lower() == "clear":
                self.default_opts = {}
            elif rest:
                self.default_opts.update(self._parse_opts(rest.split()))
            return f"default options: {self.default_opts or '(none)'}"
        if cmd == "commits":
            ids = list(self.repo._commits.keys())
            return f"{len(ids)} stored: {', '.join(ids) or '(none)'}"
        if cmd == "last":
            r = self.last
            if r is None:
                return "nothing served yet"
            return (f"last: type {r.demo_type} commit {r.commit_number} ticks={len(r.ticks)} "
                    f"events={[(e.tick_index, e.event_type.name, e.param.decode('ascii','replace')) for e in r.events]}")
        if cmd == "dump":
            self.dump_dir = rest or ("dumps" if not self.dump_dir else None)
            return f"dump dir: {self.dump_dir or '(off)'}"
        if cmd == "ollama":
            if rest.lower() in ("", "off", "0"):
                self.ollama_model = None
            else:
                self.ollama_model = rest
            return f"ollama brain for type 4: {self.ollama_model or 'off (rules only)'}"
        if cmd == "live":
            return "\n".join(self.live_log[-5:]) or "no type 4 sessions yet"
        if cmd == "status":
            return (f"forced type: {self.forced_type or 'random'} | repeat chance: {self.repeat_chance} | "
                    f"options: {self.default_opts or '(none)'} | dump: {self.dump_dir or 'off'} | "
                    f"stored commits: {len(self.repo)} | ollama: {self.ollama_model or 'off'}")
        if cmd == "list":
            return ("types: 1 twisting, 2 void/frozen+kill, 3 aimless gameplay, 4 live 'let's player' "
                    "(needs the latest plugin), 6 editor/deleter, 7 cubemap idle (5 not built -> type 2)")
        return ("commands: type <n|random> | repeat <on|off|0-1> | opts k=v ... | opts clear | "
                "commits | last | live | ollama <model|off> | dump [dir] | status | list | help")

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
    parser.add_argument("--force-type", type=int, default=None,
                        help="always serve this demo type (1-7), skipping the weighted pick")
    parser.add_argument("--no-repeat", action="store_true",
                        help="never re-serve a stored commit (otherwise 10%% of pulls repeat one)")
    parser.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE",
                        help="default generator option for every request, e.g. --opt yaw_speed=90 (repeatable)")
    parser.add_argument("--dump", metavar="DIR", default=None,
                        help="write every served recording to DIR as JSON")
    parser.add_argument("--ollama", metavar="MODEL", default=None,
                        help="give Type 4 an LLM brain from a local Ollama model, e.g. --ollama llama3")
    parser.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    parser.add_argument("--no-console", action="store_true",
                        help="don't read commands from the server window's stdin")
    args = parser.parse_args()

    configure_logging(args.debug)
    srv = InterloperServer(host=args.host, port=args.port, debug=args.debug)
    srv.forced_type = args.force_type
    if args.no_repeat:
        srv.repeat_chance = 0.0
    srv.default_opts = InterloperServer._parse_opts(args.opt)
    srv.dump_dir = args.dump
    srv.ollama_model = args.ollama
    srv.ollama_host = args.ollama_host
    if not args.no_console and sys.stdin and sys.stdin.isatty():
        threading.Thread(target=srv.console_loop, daemon=True).start()
        log.info("server console ready: type 'help' here (e.g. 'type 1', 'repeat off', 'status')")
    srv.start()


if __name__ == "__main__":
    main()
