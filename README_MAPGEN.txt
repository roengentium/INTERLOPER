INTERLOPER map generator
========================

Makes a playable Source map end to end: VMF -> checked for leaks -> vbsp/vvis/vrad -> .bsp copied
into the game's maps folder. Games: hl2 (also hl2_complete/episodic...), portal, garrysmod.

Quick start (from the interloper_server folder):
    py make_map.py --game hl2 --seed 7 --compile
    -> prints   in game:  map fsky_svr_gen_7

Three AI modes (--mode):
    procedural  code only, no AI
    theme       code builds valid geometry; the AI picks mood: style, size, darkness, lighting, fog
    llm         the AI writes the whole VMF. It is parsed, wrongly wound faces are repaired, and it
                is leak-tested; the AI is told why if rejected and gets one retry, then it falls
                back to procedural. (Expect fallbacks with small models.)
    auto        theme if an AI is configured, else procedural   (default)

Give it an AI (any of):
    --ollama llama3                         (--ollama-host http://ip:11434 for another PC)
    --llm-url https://provider/v1 --llm-model NAME     (key from env var INTERLOPER_LLM_KEY)
Mood text for the AI:  --hint "flooded maintenance wing, one light still on"

Other options:
    --size tiny|small|medium|large|huge   --style <dev|concrete|brick|plaster|sterile|dark...>
    --seed N (same seed = same map)       --name myname      --tag FSKY_SVR_GEN
    --gamedir / --bindir                  paths; defaults come from mapgen_config.json (edit it)
    --final                               slower, nicer lighting     --no-install   compile only

From the server window (uses the server's own AI settings):
    map hl2 --seed 7 --compile
    map portal --mode theme --hint "empty chamber wing" --compile

What is in the maps: grid of rooms joined by doorways, some pillars/crates, lights (some rooms
dark), optional fog, pickups for Type 4 (HL2/GMod: health/ammo/weapons; Portal: portal gun + metal boxes).
The lore tag is written to the map as worldspawn "comment" and an info_target named after it, and
used as the default map-name prefix. Change with --tag / --name.

Needs: the compile tools (vbsp.exe, vvis.exe, vrad.exe). HL2/Portal/GMod include them in their
bin folder, otherwise Source SDK Base 2013 Singleplayer has them. If it can't find them: --bindir.
If you set it up wrong the VMF is still written; open it in Hammer or compile by hand.

Honest limits:
  * Geometry, leak check, repair and the compile runner are tested here, but the real
    vbsp/vvis/vrad run has not been - those only exist on your machine. Send me the output of the
    first compile if anything fails.
  * Material names are stock content but unchecked. A wrong one shows as pink/black error textures;
    fix in mapgen/themes.py or put overrides in mapgen_textures.json.
  * Portal 2 is not supported here (different engine/tools).
  * Maps are rooms-and-doors liminal spaces, not puzzles.
