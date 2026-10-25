"""Regression: a pickup we can never take (e.g. the portal gun we already hold) must not
freeze the player. Seen live in Portal: 483 of 939 observations spent 'picking up'."""
from type4_policy import LetsPlayer, parse_obs


def obs(gt, held):
    return parse_obs(f"OBS gt={gt:.3f} x=0 y=0 z=0 yaw=0 pitch=0 hp=100 w={held} rf=800 rl=400 rr=400 "
                     f"rl2=300 rr2=300 fd=0 hc=worldspawn hd=500 ents=weapon_portalgun,10,0,0,1")


def main():
    # 1. unreachable/unremovable pickup nearby: must give up and go back to exploring + firing portals
    p = LetsPlayer(game="portal", seed=2, seconds=120.0)
    for i in range(600):
        p.step(obs(i * 0.09, "weapon_pistol"))
    assert p.stats["pickups"] < 40, p.stats
    assert p.stats["portals"] >= 1, p.stats
    # 2. the weapon in our own hands is never a pickup target
    q = LetsPlayer(game="portal", seed=2, seconds=120.0)
    for i in range(300):
        q.step(obs(i * 0.09, "weapon_portalgun"))
    assert q.stats["pickups"] == 0, q.stats
    assert q.stats["portals"] >= 1, q.stats
    print("stuck-pickup regression ok", p.stats, q.stats)


main()
