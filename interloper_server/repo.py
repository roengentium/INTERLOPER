"""
repo.py

"The Repo" - the in-memory store of committed demos, matching the ARG's own
fiction (server messages reference demos coming from a repository, and the
same commit number can be pulled multiple times to return an identical demo).

This is intentionally simple (dict + lock) for now. A persistence layer
(sqlite, disk cache of serialized Recordings) is a drop-in swap later without
touching the protocol.
"""

import random
import threading
from typing import Dict, Optional, Tuple

from recording import Recording

# Rough type distribution. Type 1 ("Twisting") is documented as ~90% of a
# real sample (I1), everything else is comparatively rare.
TYPE_WEIGHTS = {
    1: 90,
    2: 3,
    3: 2,
    4: 1,
    5: 1,
    6: 2,
    7: 1,
}


# Games where "real gameplay" (type 4) is what the lore says you mostly get:
# Portal is built around puzzles, so its demos are mostly someone actually playing.
GAME_TYPE_WEIGHTS = {
    "portal": {1: 25, 2: 3, 3: 4, 4: 60, 6: 2, 7: 1},
    "portal2": {1: 25, 2: 3, 3: 4, 4: 60, 6: 2, 7: 1},
}


class Repo:
    def __init__(self):
        self._commits: Dict[str, Recording] = {}
        self._lock = threading.Lock()

    def pick_type(self, rng: Optional[random.Random] = None, game: Optional[str] = None) -> int:
        rng = rng or random
        table = GAME_TYPE_WEIGHTS.get(str(game).lower(), TYPE_WEIGHTS) if game else TYPE_WEIGHTS
        types = list(table.keys())
        weights = list(table.values())
        return rng.choices(types, weights=weights, k=1)[0]

    @staticmethod
    def new_commit_id(rng: Optional[random.Random] = None) -> str:
        rng = rng or random
        return f"{rng.randint(0,99):02d}-{rng.randint(0,99):02d}-{rng.randint(0,99):02d}"

    def commit(self, recording: Recording, commit_id: Optional[str] = None) -> str:
        commit_id = commit_id or recording.commit_number or self.new_commit_id()
        with self._lock:
            self._commits[commit_id] = recording
        return commit_id

    def pull(self, commit_id: str) -> Optional[Recording]:
        with self._lock:
            return self._commits.get(commit_id)

    def pull_random_existing(self, repeat_chance: float = 0.1) -> Tuple[Optional[str], Optional[Recording]]:
        """With some probability, re-serve a previously committed demo instead
        of generating a new one. Models the lore detail that the same commit
        number can return an identical demo more than once."""
        with self._lock:
            if self._commits and random.random() < repeat_chance:
                commit_id = random.choice(list(self._commits.keys()))
                return commit_id, self._commits[commit_id]
        return None, None

    def __len__(self):
        with self._lock:
            return len(self._commits)
