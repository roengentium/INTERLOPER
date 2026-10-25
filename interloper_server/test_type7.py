"""Sanity test for the Type 7 generator. Run: python test_type7.py"""
from recording import CommitFormat, EventType, Recording
from generators.type2 import MapBounds
from generators.type7 import generate_type7, TICKRATE

BOUNDS = MapBounds(mins=(-1000.0, -1000.0, 0.0), maxs=(1000.0, 1000.0, 500.0))
N = 200
for seed in range(N):
    r = generate_type7(BOUNDS, seed=seed)
    n = len(r.ticks)
    assert 19.0 <= n / TICKRATE <= 61.0
    assert r.demo_type == 7 and r.commit_format == CommitFormat.TODAY and r.commit_number == "TODAY"
    # perfectly stationary, no input
    assert all(t.origin == r.ticks[0].origin and t.angles == r.ticks[0].angles
               and t.buttons == 0 and t.move == (0.0, 0.0, 0.0) for t in r.ticks)
    assert all(BOUNDS.mins[a] <= r.ticks[0].origin[a] <= BOUNDS.maxs[a] for a in range(3))
    # weapon first, then one repeated chime
    assert r.events[0].event_type == EventType.WEAPON_EQUIP and r.events[0].param == b"weapon_cubemap"
    chimes = [e for e in r.events if e.event_type == EventType.SOUND]
    assert chimes and len({e.param for e in chimes}) == 1 and chimes[0].param.startswith(b"chime:")
    gaps = {b.tick_index - a.tick_index for a, b in zip(chimes, chimes[1:])}
    assert len(gaps) <= 1, "chime interval must be constant"
    assert all(0 <= e.tick_index < n for e in r.events)
    assert generate_type7(BOUNDS, seed=seed).serialize() == r.serialize()
    rt = Recording.deserialize(r.serialize())
    assert rt.commit_format == CommitFormat.TODAY and rt.commit_number == "TODAY" and len(rt.ticks) == n
print(f"{N} seeds OK")
