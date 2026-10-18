# Interloper (Source Engine Mod)

Talks to the core Python server (`interloper-server/`) over TCP and plays
back the demo it receives, live, while the engine's own `record` is capturing
a real `.dem` file.

## Build requirements (not included in this environment)

- `hl2sdk-orangebox` (or any other sdks) (Source 2013)
- Metamod:Source SDK matching the 1.6-era "core" API 
- A build setup pointing at:
  - `public/`, `public/engine/`, `public/tier0/`, `public/tier1/`
  - `game/server/` (for `cbase.h`, `player.h`, and friends)
