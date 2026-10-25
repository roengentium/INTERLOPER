"""
type4_profiles.py

Per-game knowledge for the Type 4 ("real gameplay") live player: which
entity classes are enemies, pickups, things to press, and how the game's
weapon behaves. The plugin sends the game folder name (game=portal, game=tf,
game=hl2...), the server picks a profile here. Unknown games get the generic
profile (explore, look around, press things, no combat).
"""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class Profile:
    name: str
    can_fight: bool = False
    hostile_prefixes: Tuple[str, ...] = ()
    friendly_prefixes: Tuple[str, ...] = ()
    item_prefixes: Tuple[str, ...] = ("item_",)
    use_prefixes: Tuple[str, ...] = ("func_button", "prop_button")
    carry_prefixes: Tuple[str, ...] = ()
    portal_gun: bool = False
    attack_range: float = 900.0
    weapon_cmds: Tuple[str, ...] = ()       # commands used to switch weapons
    sprint: bool = True
    eye_height: float = 64.0


GENERIC = Profile(name="generic")

HL2 = Profile(
    name="hl2",
    can_fight=True,
    hostile_prefixes=(
        "npc_zombie", "npc_fastzombie", "npc_poisonzombie", "npc_zombine",
        "npc_headcrab", "npc_antlion", "npc_combine_s", "npc_metropolice",
        "npc_manhack", "npc_rollermine", "npc_hunter", "npc_cscanner",
        "npc_clawscanner", "npc_turret_ceiling", "npc_sniper", "npc_stalker",
    ),
    friendly_prefixes=("npc_citizen", "npc_alyx", "npc_barney", "npc_kleiner",
                       "npc_eli", "npc_dog", "npc_monk", "npc_vortigaunt", "npc_gman"),
    item_prefixes=("item_healthkit", "item_healthvial", "item_battery", "item_ammo",
                   "item_box", "item_suit", "weapon_"),
    use_prefixes=("func_button", "prop_door_rotating", "func_door_rotating"),
    attack_range=1000.0,
    weapon_cmds=("slot2", "slot3", "slot2", "slot1", "slot3", "slot4"),
)

PORTAL = Profile(
    name="portal",
    can_fight=False,
    hostile_prefixes=(),
    item_prefixes=("weapon_portalgun",),
    use_prefixes=("func_button", "prop_button", "prop_floor_button", "prop_under_button",
                  "func_door"),
    carry_prefixes=("prop_weighted_cube", "prop_physics", "prop_monster_box"),
    portal_gun=True,
    sprint=False,
)

TF2 = Profile(
    name="tf",
    can_fight=True,
    hostile_prefixes=("player", "npc_", "tf_bot", "obj_sentrygun"),
    item_prefixes=("item_healthkit", "item_ammopack", "tf_ammo_pack"),
    use_prefixes=(),
    attack_range=900.0,
    weapon_cmds=("slot1", "slot2", "slot1", "slot3"),
)

PROFILES = {
    "hl2": HL2, "hl2_complete": HL2, "episodic": HL2, "ep2": HL2, "lostcoast": HL2,
    "hl2mp": HL2, "hl1": HL2, "garrysmod": HL2,
    "portal": PORTAL, "portal2": PORTAL,
    "tf": TF2,
}


def profile_for(game):
    if not game:
        return GENERIC
    return PROFILES.get(str(game).lower(), GENERIC)
