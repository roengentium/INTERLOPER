"""Sanity/stress test for the Type 6 generator. Run: python test_type6.py"""
import statistics
from collections import Counter

from recording import EventType, Recording
from generators.type2 import MapBounds
from generators.type6 import generate_type6, TICKRATE

BOUNDS = MapBounds(mins=(-4096.0, -4096.0, -512.0), maxs=(4096.0, 4096.0, 2048.0))
N = 150
secs, locks, deletes, sounds, flickers = [], [], [], [], []
kinds = Counter()

for seed in range(N):
    r = generate_type6(BOUNDS, seed=seed, origin=(100.0, 200.0, 300.0))
    n = len(r.ticks)
    assert 24.0 <= n / TICKRATE <= 76.0, (seed, n / TICKRATE)
    assert r.demo_type == 6
    assert all(t.buttons == 0 for t in r.ticks)
    assert all(-89.0 <= t.angles[0] <= 89.0 and 0.0 <= t.angles[1] < 360.0 for t in r.ticks)
    assert [e.tick_index for e in r.events] == sorted(e.tick_index for e in r.events)
    assert all(0 <= e.tick_index < n for e in r.events)
    assert r.ticks[0].origin == (100.0, 200.0, 300.0)

    seen_lock = False
    for e in r.events:
        if e.event_type == EventType.ENTITY_DELETE:
            if e.param.startswith(b"lock"):
                seen_lock = True
                kinds[e.param] += 1
            else:
                assert e.param == b"delete" and seen_lock, "delete without an earlier lock"
        else:
            assert e.event_type == EventType.SOUND and e.param == b"friends/friend_online.wav"
    secs.append(n / TICKRATE)
    locks.append(sum(1 for e in r.events if e.param.startswith(b"lock")))
    deletes.append(sum(1 for e in r.events if e.event_type == EventType.ENTITY_DELETE and e.param == b"delete"))
    sounds.append(sum(1 for e in r.events if e.event_type == EventType.SOUND))

    # machine precision: during a lock the tick right before a delete matches the one before it exactly
    for e in r.events:
        if e.event_type == EventType.ENTITY_DELETE and e.param == b"delete" and e.tick_index >= 1:
            assert r.ticks[e.tick_index].angles == r.ticks[e.tick_index - 1].angles
            assert r.ticks[e.tick_index].origin == r.ticks[e.tick_index - 1].origin

    assert generate_type6(BOUNDS, seed=seed, origin=(100.0, 200.0, 300.0)).serialize() == r.serialize()
    rt = Recording.deserialize(r.serialize())
    assert len(rt.ticks) == n and len(rt.events) == len(r.events)

r = generate_type6(BOUNDS, seed=1)
assert len(r.ticks) > 1
print(f"{N} seeds OK")
print(f"duration median {statistics.median(secs):.0f}s (min {min(secs):.0f}, max {max(secs):.0f})")
print(f"locks/demo avg {statistics.mean(locks):.1f}, deletes/demo avg {statistics.mean(deletes):.1f}, sound events/demo avg {statistics.mean(sounds):.1f}")
print(f"lock kinds: {dict(kinds)}")
