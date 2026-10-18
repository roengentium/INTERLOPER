-- INTERLOPER demo player for Garry's Mod (server-side, no binary modules).
--
-- Reads data/interloper/demo_<type>.json (written by export_json.py), then plays it
-- back on the player who typed `interlope`. Types 1 and 2 move/aim the player
-- directly; type 3 replays the recorded buttons and view angles as real input,
-- so the engine does the walking, colliding and shooting.
--
-- Type 6 ("editor behavior") hunts real entities: on a lock event it picks a
-- nearby prop/NPC, teleports next to it if needed, locks the camera on it, and
-- the delete event erases it with the Kill input, like `ent_fire !picker kill`.
--
-- Type 7 teleports you to a random map spawn point, equips weapon_cubemap,
-- stands you perfectly still and repeats one chime. It needs built cubemaps:
-- run `interlope_prepare` once per map (it runs `buildcubemaps`).
--
-- Console commands:  interlope [1|2|3|6|7]   interlope_stop   interlope_prepare
--                    (no number = random pick from the demos that exist)
-- ConVar:            interlope_autorecord (1 = start `record` automatically)

if not SERVER then return end

local DATA_DIR = "interloper/"
local LEGACY_FILE = DATA_DIR .. "demo.json"
local autorecord = CreateConVar("interlope_autorecord", "1", FCVAR_ARCHIVE,
    "Automatically run `record` when an Interloper demo starts")

-- Event types, same numbers as EventType in recording.py
local EV_KILL, EV_SOUND, EV_WEAPON, EV_DELETE, EV_SPAWN = 0, 1, 2, 3, 4

local state = nil

-- Closes the console / menu on the player's screen so commands drop straight
-- into the game. Needs the small client file (autorun/client/interloper_cl.lua).
util.AddNetworkString("interloper_hide_ui")
local function hideUI(ply)
    if not IsValid(ply) then return end
    timer.Simple(0.05, function()
        if IsValid(ply) then
            net.Start("interloper_hide_ui")
            net.Send(ply)
        end
    end)
end

-- The deletion sound the ARG's Type 6 demos use. Put the file at
-- <addon>/sound/friends/friend_online.wav (or anywhere in the game's sound folder).
local DELETE_SOUND = "friends/friend_online.wav"
if file.Exists("sound/" .. DELETE_SOUND, "GAME") then
    resource.AddFile("sound/" .. DELETE_SOUND)   -- let multiplayer clients download it
end
local TELEPORT_IF_FARTHER_THAN = 700

-- Type 7 chime sounds. A recording says "chime:N" and N picks from this list
-- (wrapping around). Drop the real chime at <addon>/sound/interloper/chime.wav
-- and it is used automatically. Until then these two HL2 bells are stand-ins.
local CHIME_SOUNDS = {
    "ambient/misc/brass_bell_C.wav",
    "ambient/misc/brass_bell_F.wav",
}
if file.Exists("sound/interloper/chime.wav", "GAME") then
    CHIME_SOUNDS = { "interloper/chime.wav" }
    resource.AddFile("sound/interloper/chime.wav")
end

-- Stand-in for weapon_cubemap on games that lack it: the same six-ball model,
-- floated in front of the view. Tune distance/scale if it looks off.
local ENVBALLS_MODEL = "models/shadertest/envballs.mdl"
local BALLS_DISTANCE = 70
local BALLS_DROP = 8

local function say(ply, msg)
    msg = "[INTERLOPE] " .. msg
    print(msg)
    if IsValid(ply) then ply:PrintMessage(HUD_PRINTCONSOLE, msg) end
end

local function stop(reason)
    if not state then return end
    local ply = state.ply
    if IsValid(state.balls) then state.balls:Remove() end
    if IsValid(ply) and ply:HasWeapon("weapon_cubemap") then
        ply:StripWeapon("weapon_cubemap")   -- the spheres leave with the demo
    end
    if IsValid(ply) then
        ply:SetMoveType(MOVETYPE_WALK)
        if state.recording then ply:ConCommand("stop") end
    end
    say(ply, reason or "finished")
    state = nil
end

local function isCandidate(ply, ent)
    if not IsValid(ent) or ent == ply or ent:IsPlayer() or ent:IsWorld() then return false end
    if state.done[ent] then return false end
    if IsValid(ent:GetOwner()) or IsValid(ent:GetParent()) then return false end
    local class = ent:GetClass()
    return ent:IsNPC() or ent:IsNextBot()
        or string.sub(class, 1, 5) == "prop_"
        or string.sub(class, 1, 5) == "item_"
        or class == "func_physbox"
end

local function findTarget(ply, preferMoving)
    local from = ply:GetPos()
    local best, bestScore
    for _, ent in ipairs(ents.GetAll()) do
        if isCandidate(ply, ent) then
            local score = from:DistToSqr(ent:WorldSpaceCenter())
            if preferMoving and ent:GetVelocity():LengthSqr() > 100 then
                score = score * 0.05
            end
            if not bestScore or score < bestScore then
                best, bestScore = ent, score
            end
        end
    end
    return best
end

local function lockOn(ply, preferMoving)
    local target = findTarget(ply, preferMoving)
    if not target then
        state.lock = nil
        return
    end

    -- Too far away? Teleport next to it and carry the rest of the flight path
    -- on from there.
    local tpos = target:WorldSpaceCenter()
    if ply:GetPos():Distance(tpos) > TELEPORT_IF_FARTHER_THAN then
        local away = ply:GetPos() - tpos
        away.z = 0
        if away:LengthSqr() < 1 then away = Vector(1, 0, 0) end
        away:Normalize()
        local newPos = tpos + away * math.random(250, 450) + Vector(0, 0, math.random(0, 80))
        ply:SetPos(newPos)
        state.base = newPos - state.rel
    end
    state.lock = target
end

local function deleteTarget(ply)
    local target = state.lock
    if not IsValid(target) then
        local tr = ply:GetEyeTrace()
        target = IsValid(tr.Entity) and not tr.Entity:IsWorld() and tr.Entity or nil
    end
    state.lock = nil
    if not IsValid(target) or target == ply then return end

    -- Same effect as the console command the demos use, `ent_fire !picker kill`:
    -- the Kill input erases the entity outright (NPCs vanish, no death animation).
    state.done[target] = true
    target:Fire("kill")
end

local function fireEvent(ev)
    local ply, kind, param = state.ply, ev[2], ev[3]
    if kind == EV_KILL then
        ply:SetMoveType(MOVETYPE_WALK)
        ply:Kill()
        stop("player killed, demo over")
    elseif kind == EV_WEAPON then
        if string.sub(param, 1, 4) == "slot" then
            ply:ConCommand(param)
        else
            ply:Give(param)
            if param == "weapon_cubemap" then
                if ply:HasWeapon(param) then
                    -- give it a moment to be fully equipped, then draw it
                    timer.Simple(0.3, function()
                        if IsValid(ply) and ply:HasWeapon(param) then
                            ply:SelectWeapon(param)
                            ply:ConCommand("use weapon_cubemap")
                        end
                    end)
                elseif util.IsValidModel(ENVBALLS_MODEL) then
                    say(ply, "weapon_cubemap is not available here - showing the envballs model in front of you instead")
                    local balls = ents.Create("prop_dynamic")
                    balls:SetModel(ENVBALLS_MODEL)
                    balls:SetSolid(SOLID_NONE)
                    balls:Spawn()
                    state.balls = balls
                else
                    say(ply, "weapon_cubemap and the envballs model are both unavailable in this game")
                end
            end
        end
    elseif kind == EV_SOUND then
        local sound = param
        if param == "delete" then
            sound = DELETE_SOUND                       -- older exports
        elseif string.sub(param, 1, 6) == "chime:" then
            local n = tonumber(string.sub(param, 7)) or 0
            sound = CHIME_SOUNDS[(n % #CHIME_SOUNDS) + 1]
        end
        ply:EmitSound(sound)
    elseif kind == EV_DELETE then
        if string.sub(param, 1, 4) == "lock" then
            lockOn(ply, param == "lock:moving")
        elseif param == "delete" then
            deleteTarget(ply)
        end
    elseif kind == EV_SPAWN then
        say(ply, "spawn events are not implemented yet, skipped")
    end
end

local function pickFile(wanted)
    if wanted then
        return DATA_DIR .. "demo_" .. wanted .. ".json"
    end
    local found = {}
    for n = 1, 7 do
        local path = DATA_DIR .. "demo_" .. n .. ".json"
        if file.Exists(path, "DATA") then found[#found + 1] = path end
    end
    if #found > 0 then return found[math.random(#found)] end
    return LEGACY_FILE
end

local function start(ply, wanted)
    if state then stop("restarted") end

    local path = pickFile(wanted)
    local raw = file.Read(path, "DATA")
    if not raw then
        say(ply, "no demo file (" .. path .. "). Run: py export_json.py --all --gmod <your garrysmod folder>")
        return
    end
    local d = util.JSONToTable(raw)
    if not d or not d.ticks or #d.ticks == 0 then
        say(ply, "demo file is empty or invalid")
        return
    end

    -- Type 7 spawns the player at a random location: use a random spawn point
    -- of the map (a safe, reachable spot). No spawn points = stay where you are.
    if d.type == 7 then
        local spawns = ents.FindByClass("info_player_*")
        if #spawns > 0 then
            ply:SetPos(spawns[math.random(#spawns)]:GetPos())
            ply:SetLocalVelocity(vector_origin)
        end
    end

    local first = d.ticks[1]
    state = {
        ply = ply,
        data = d,
        i = 1,
        ev = 1,
        base = ply:GetPos(),
        first = Vector(first[1], first[2], first[3]),
        input = (d.type == 3),
        recording = false,
        done = {},
        lock = nil,
        rel = vector_origin,
    }

    -- Types 1, 2 and 6 place the player directly, so noclip (no gravity, no
    -- collision) until the demo ends. Types 3 and 7 keep normal physics.
    if not state.input and d.type ~= 7 then ply:SetMoveType(MOVETYPE_NOCLIP) end

    if autorecord:GetBool() then
        ply:ConCommand("record type" .. d.type .. "_" .. tostring(d.commit))
        state.recording = true
    end

    say(ply, ("playing type %d, COMMITTED %s, %d ticks"):format(d.type, tostring(d.commit), #d.ticks))
    if d.type == 7 then
        say(ply, "type 7 needs built cubemaps. If the balls look flat or black, run interlope_prepare once on this map.")
    end
end

hook.Add("StartCommand", "interloper_playback", function(ply, cmd)
    if not state or ply ~= state.ply then return end
    if not IsValid(ply) or not ply:Alive() then
        stop("player died or left")
        return
    end

    local d = state.data
    local t = d.ticks[state.i]
    if not t then
        stop("finished")
        return
    end

    -- offset from the recording's first position (used by the relative types)
    state.rel = Vector(t[1], t[2], t[3]) - state.first

    -- fire every event scheduled for this tick or earlier
    while state and d.events[state.ev] and d.events[state.ev][1] < state.i do
        local ev = d.events[state.ev]
        state.ev = state.ev + 1
        fireEvent(ev)
    end
    if not state then return end

    local ang = Angle(t[4], t[5], t[6])

    -- Stand-in cubemap balls follow the view.
    if IsValid(state.balls) then
        local a = Angle(t[4], t[5], t[6])
        state.balls:SetPos(ply:EyePos() + a:Forward() * BALLS_DISTANCE - a:Up() * BALLS_DROP)
        state.balls:SetAngles(Angle(0, t[5] + 180, 0))
    end

    -- A locked target overrides the recorded aim and is tracked every tick,
    -- so the camera follows moving objects.
    if IsValid(state.lock) then
        ang = (state.lock:WorldSpaceCenter() - ply:EyePos()):Angle()
    end

    -- Whatever the player is pressing is thrown away for the whole demo, so
    -- pausing, unpausing or touching the keyboard cannot move them.
    cmd:ClearButtons()
    cmd:ClearMovement()

    if state.input then
        -- type 3: real input. IN_* bits match the recording's button bits.
        local buttons = t[7]
        local speed = bit.band(buttons, IN_SPEED) ~= 0 and ply:GetRunSpeed() or ply:GetWalkSpeed()
        cmd:SetButtons(buttons)
        cmd:SetForwardMove(t[8] * speed)
        cmd:SetSideMove(t[9] * speed)
        cmd:SetViewAngles(ang)
    elseif d.type == 7 then
        -- type 7: stand perfectly still with a fixed view.
        ply:SetLocalVelocity(vector_origin)
        ply:SetEyeAngles(ang)
        cmd:SetViewAngles(ang)
    else
        -- types 1, 2 and 6: place the player. Type 2 uses the recorded absolute
        -- position (out of bounds); type 1 moves relative to where the player
        -- already stands.
        local pos
        if d.type == 2 then
            pos = Vector(t[1], t[2], t[3])
        else
            pos = state.base + state.rel
        end
        ply:SetPos(pos)
        ply:SetLocalVelocity(vector_origin)
        ply:SetEyeAngles(ang)
        cmd:SetViewAngles(ang)
    end

    state.i = state.i + 1
end)

concommand.Add("interlope", function(ply, cmd, args)
    if not IsValid(ply) then
        print("[INTERLOPE] run this from in-game, not the server console")
        return
    end
    if not game.SinglePlayer() and not ply:IsSuperAdmin() then
        say(ply, "superadmin only on multiplayer servers")
        return
    end
    local wanted = tonumber(args[1])
    if args[1] and not wanted then
        say(ply, "usage: interlope [1|2|3|6|7]")
        return
    end
    hideUI(ply)
    start(ply, wanted)
end)

concommand.Add("interlope_prepare", function(ply)
    if not IsValid(ply) then return end
    if not game.SinglePlayer() and not ply:IsSuperAdmin() then
        say(ply, "superadmin only on multiplayer servers")
        return
    end
    say(ply, "running buildcubemaps on " .. game.GetMap() .. " - let it finish (the map may reload), then run interlope 7")
    hideUI(ply)
    ply:ConCommand("buildcubemaps")
end)

concommand.Add("interlope_stop", function(ply)
    hideUI(ply)
    if state then stop("stopped by user") end
end)
