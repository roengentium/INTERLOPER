// interloper_plugin.cpp
//
// See interloper_plugin.h for what changed and what's still unverified.

#include "interloper_plugin.h"

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <cmath>
#include <cctype>
#include <iplayerinfo.h>
#include <engine/IEngineTrace.h>
#include <bspflags.h>

// <windows.h> (pulled in by winsock) turns GetClassName into GetClassNameA,
// which would break edict_t::GetClassName() below. The SDK headers were parsed
// before the macro existed, so undefine it here.
#ifdef GetClassName
#undef GetClassName
#endif

CInterloperPlugin g_InterloperPlugin;
PLUGIN_EXPOSE(CInterloperPlugin, g_InterloperPlugin);

// Engine-side interfaces, resolved in Load(). Names match what
// engine_wrappers.h expects (it declares `extern IVEngineServer *engine;`
// and `extern CGlobalVars *gpGlobals;`).
IVEngineServer *engine = nullptr;
IServerGameDLL *server = nullptr;
IVModelInfo *modelinfo = nullptr;
CGlobalVars *gpGlobals = nullptr;
ICvar *icvar = nullptr;
static IPlayerInfoManager *s_playerinfo = nullptr;
static IEngineTrace *s_trace = nullptr;
static IServerGameEnts *s_gameents = nullptr;

// Last path component of the game directory ("tf", "hl2", "hl2_complete",
// "hl1", "garrysmod", ...). Lower-cased copy in a static buffer.
static const char *GameFolder()
{
	static char folder[128];
	char full[260] = {0};
	engine->GetGameDir(full, sizeof(full));
	const char *last = full;
	for (const char *p = full; *p; p++)
		if (*p == '/' || *p == '\\')
			last = p + 1;
	size_t i = 0;
	for (; last[i] && i < sizeof(folder) - 1; i++)
		folder[i] = (char)tolower((unsigned char)last[i]);
	folder[i] = 0;
	return folder;
}
static bool IsTF2() { return strcmp(GameFolder(), "tf") == 0; }


static ConVar *g_pDebugCvar = nullptr;
static ConVar *g_pPortCvar = nullptr;
static ConCommand *g_pInterlopeCmd = nullptr;
static ConCommand *g_pStopCmd = nullptr;
static ConCommand *g_pStatusCmd = nullptr;
static ConVar *g_pTypeCvar = nullptr;    // 0 = server picks, 1-7 = ask for that type
static ConVar *g_pOptsCvar = nullptr;    // extra "key=value ..." sent with every request
static ConVar *g_pCheatsCvar = nullptr;  // 0 leave sv_cheats alone, 1 enable for the demo then restore, 2 enable and leave on
static ConVar *g_pRecordCvar = nullptr;  // 0 = play but do not record a .dem
static ConVar *g_pNoclipCvar = nullptr;  // 0 = never toggle noclip
static ConVar *g_pDryRunCvar = nullptr;  // 1 = fetch + print the recording, do not play it
static ConVar *g_pHostCvar = nullptr;    // core server address
static ConVar *g_pNameCvar = nullptr;    // demo file name override

// Playback gating: we don't begin applying ticks (or start recording) until
// the server is actually simulating, i.e. no longer paused by the console.
static bool s_playbackStarted = false;
static int  s_pausedFrames = 0;
static const int UNPAUSE_RETRY_INTERVAL = 5;   // frames between unpause retries
static const int UNPAUSE_GIVE_UP_FRAMES = 300; // then start anyway

// Button bits match the SDK's IN_* flags (see Button in recording.py).
// Held-state is driven with +/- console commands, so it goes through the
// same input path as a real keyboard.
struct ButtonBinding
{
	uint32_t mask;
	const char *press;
	const char *release;
};

static const ButtonBinding s_buttonBindings[] =
{
	{ 1u << 0,  "+attack\n",    "-attack\n"    },
	{ 1u << 1,  "+jump\n",      "-jump\n"      },
	{ 1u << 2,  "+duck\n",      "-duck\n"      },
	{ 1u << 3,  "+forward\n",   "-forward\n"   },
	{ 1u << 4,  "+back\n",      "-back\n"      },
	{ 1u << 5,  "+use\n",       "-use\n"       },
	{ 1u << 9,  "+moveleft\n",  "-moveleft\n"  },
	{ 1u << 10, "+moveright\n", "-moveright\n" },
	{ 1u << 11, "+attack2\n",   "-attack2\n"   },
	{ 1u << 13, "+reload\n",    "-reload\n"    },
	{ 1u << 17, "+speed\n",     "-speed\n"     },
};

// Same registration pattern s1_sample_mm uses for its ConVar - required or
// ConCommand/ConVar objects constructed via `new` never actually register
// with the engine. This was missing entirely from an earlier draft.
class BaseAccessor : public IConCommandBaseAccessor
{
public:
	bool RegisterConCommandBase(ConCommandBase *pCommandBase)
	{
		return META_REGBASECMD(pCommandBase);
	}
} s_BaseAccessor;

// Throws every known unpause route at the engine. Which one actually sticks
// varies by engine branch and by what opened the pause (console vs. GameUI
// vs. sv_pausable state), so we send all of them and let whichever works win.
//   1. gameui_hide            - close the GameUI layer that issued the pause
//   2. unpause (client)       - normal client-side route, filtered
//   3. unpause nomsg (client) - variant that skips the "unpaused" message
//   4. unpause (server)       - bypasses the client command filter entirely
//   5. setpause 0 (server)    - forces pause state off on builds that have it
static void SendUnpause(edict_t *pPlayer)
{
	if (pPlayer)
	{
		engine->ClientCommand(pPlayer, "gameui_hide\n");
		engine->ClientCommand(pPlayer, "unpause\n");
		engine->ClientCommand(pPlayer, "unpause nomsg\n");
	}
	engine->ServerCommand("unpause\n");
	engine->ServerCommand("setpause 0\n");
}

CInterloperPlugin::CInterloperPlugin() :
	m_GameFrame(&IServerGameDLL::GameFrame, this, nullptr, &CInterloperPlugin::Hook_GameFrame)
{
}

// -- lifecycle --------------------------------------------------------

bool CInterloperPlugin::Load(PluginId id, ISmmAPI *ismm, char *error, size_t maxlen, bool late)
{
	PLUGIN_SAVEVARS();

	GET_V_IFACE_CURRENT(GetEngineFactory, engine, IVEngineServer, INTERFACEVERSION_VENGINESERVER);
	GET_V_IFACE_CURRENT(GetEngineFactory, icvar, ICvar, CVAR_INTERFACE_VERSION);
	GET_V_IFACE_CURRENT(GetEngineFactory, modelinfo, IVModelInfo, VMODELINFO_SERVER_INTERFACE_VERSION);
	GET_V_IFACE_ANY(GetServerFactory, server, IServerGameDLL, INTERFACEVERSION_SERVERGAMEDLL);

	if (!engine || !server || !modelinfo)
	{
		snprintf(error, maxlen, "failed to resolve required engine interfaces");
		return false;
	}

	// Bug fix vs. an earlier draft: gpGlobals was referenced (via
	// engine_wrappers.h and StartInterlope's use of gpGlobals->mapname)
	// but never actually assigned. Matches what s1_sample_mm does.
	gpGlobals = ismm->GetCGlobals();

	// Optional: lets us warn when the player is dead/spectating (setpos does
	// nothing then). Not fatal if the engine doesn't expose it.
	{
		CreateInterfaceFn serverFactory = ismm->GetServerFactory(false);
		s_playerinfo = serverFactory
			? (IPlayerInfoManager *)serverFactory(INTERFACEVERSION_PLAYERINFOMANAGER, nullptr)
			: nullptr;
		s_gameents = serverFactory
			? (IServerGameEnts *)serverFactory(INTERFACEVERSION_SERVERGAMEENTS, nullptr)
			: nullptr;
		CreateInterfaceFn engineFactory = ismm->GetEngineFactory(false);
		s_trace = engineFactory
			? (IEngineTrace *)engineFactory(INTERFACEVERSION_ENGINETRACE_SERVER, nullptr)
			: nullptr;
	}

	m_GameFrame.Add(server);

	g_pInterlopeCmd = new ConCommand("interlope", CC_Interlope,
		"Requests a demo from the Interloper core server", FCVAR_NONE);

	g_pDebugCvar = new ConVar("interlope_debug", "0", FCVAR_NONE,
		"Verbose Interloper logging: request/response, per-tick state, event firing");

	g_pPortCvar = new ConVar("interlope_port", "27500", FCVAR_NONE,
		"TCP port of the Interloper core server (must match server.py --port)", true, 1.0f, true, 65535.0f);

	g_pTypeCvar = new ConVar("interlope_type", "0", FCVAR_NONE,
		"Demo type to request: 0 = server picks (weighted random), 1-7 = that type. "
		"Same as 'interlope <type>'.", true, 0.0f, true, 7.0f);
	g_pOptsCvar = new ConVar("interlope_opts", "", FCVAR_NONE,
		"Extra generator options sent with every request, e.g. \"yaw_speed=90 pos=void\"");
	g_pCheatsCvar = new ConVar("interlope_cheats", "1", FCVAR_NONE,
		"0 = leave sv_cheats alone, 1 = turn it on for the demo and restore it after, 2 = turn on and leave on",
		true, 0.0f, true, 2.0f);
	g_pRecordCvar = new ConVar("interlope_record", "1", FCVAR_NONE,
		"1 = record a .dem while playing, 0 = play only (handy for testing)", true, 0.0f, true, 1.0f);
	g_pNoclipCvar = new ConVar("interlope_noclip", "1", FCVAR_NONE,
		"1 = toggle noclip on for teleport-style demos (types 1, 2, 6, 7), 0 = never touch noclip",
		true, 0.0f, true, 1.0f);
	g_pDryRunCvar = new ConVar("interlope_dryrun", "0", FCVAR_NONE,
		"1 = fetch and print the demo but do not play it", true, 0.0f, true, 1.0f);
	g_pHostCvar = new ConVar("interlope_host", "127.0.0.1", FCVAR_NONE,
		"Address of the Interloper core server");
	g_pNameCvar = new ConVar("interlope_name", "", FCVAR_NONE,
		"Demo file name (without .dem). Empty = type<N>_<commit>");

	g_pStopCmd = new ConCommand("interlope_stop", CC_InterlopeStop,
		"Abort a running Interloper demo and stop recording", FCVAR_NONE);
	g_pStatusCmd = new ConCommand("interlope_status", CC_InterlopeStatus,
		"Print Interloper settings and the state of the current/last demo", FCVAR_NONE);

	// Register everything created via `new ConCommand(...)`/`new ConVar(...)`
	// above - this is the step an earlier draft skipped, meaning
	// "interlope" likely would never have actually worked in-engine.
#if SOURCE_ENGINE >= SE_ORANGEBOX
	g_pCVar = icvar;
	ConVar_Register(0, &s_BaseAccessor);
#else
	ConCommandBaseMgr::OneTimeInit(&s_BaseAccessor);
#endif

	META_LOG(g_PLAPI, "Interloper plugin loaded (console-command mode, engine-agnostic)");
	return true;
}

bool CInterloperPlugin::Unload(char *error, size_t maxlen)
{
	m_GameFrame.Remove(server);

	delete g_pInterlopeCmd;
	g_pInterlopeCmd = nullptr;

	delete g_pDebugCvar;
	g_pDebugCvar = nullptr;

	delete g_pPortCvar;
	g_pPortCvar = nullptr;

	delete g_pStopCmd;    g_pStopCmd = nullptr;
	delete g_pStatusCmd;  g_pStatusCmd = nullptr;
	delete g_pTypeCvar;   g_pTypeCvar = nullptr;
	delete g_pOptsCvar;   g_pOptsCvar = nullptr;
	delete g_pCheatsCvar; g_pCheatsCvar = nullptr;
	delete g_pRecordCvar; g_pRecordCvar = nullptr;
	delete g_pNoclipCvar; g_pNoclipCvar = nullptr;
	delete g_pDryRunCvar; g_pDryRunCvar = nullptr;
	delete g_pHostCvar;   g_pHostCvar = nullptr;
	delete g_pNameCvar;   g_pNameCvar = nullptr;

	return true;
}

bool CInterloperPlugin::Pause(char *error, size_t maxlen) { return true; }
bool CInterloperPlugin::Unpause(char *error, size_t maxlen) { return true; }

// -- debug logging --------------------------------------------------------

void CInterloperPlugin::DebugLog(const char *fmt, ...)
{
	if (!g_pDebugCvar || g_pDebugCvar->GetInt() == 0)
		return;

	char buf[512];
	va_list args;
	va_start(args, fmt);
	vsnprintf(buf, sizeof(buf), fmt, args);
	va_end(args);

	META_CONPRINTF("[interlope debug] %s\n", buf);
}

// -- console command ---------------------------------------------------

// Everything typed after the command name, joined with spaces.
#if SOURCE_ENGINE >= SE_ORANGEBOX
static std::string CollectArgs(const CCommand &args)
{
	std::string out;
	for (int i = 1; i < args.ArgC(); i++)
	{
		if (!out.empty()) out += ' ';
		out += args.Arg(i);
	}
	return out;
}
#else
static std::string CollectArgs()
{
	std::string out;
	for (int i = 1; i < engine->Cmd_Argc(); i++)
	{
		if (!out.empty()) out += ' ';
		out += engine->Cmd_Argv(i);
	}
	return out;
}
#endif

static void PrintUsage()
{
	META_CONPRINT(
		"interlope [type] [key=value ...] [dry] [norec]\n"
		"  interlope            random weighted pick (or interlope_type)\n"
		"  interlope 1          demo type 1 (2, 3, 6, 7 also built)\n"
		"  interlope 1 yaw_speed=90 pos=void calm=0 ticks=200 lead=20 tail=20 seed=5\n"
		"  interlope 3 style=... seconds=60      interlope commit=13-70-18 (replay stored)\n"
		"  dry   = fetch and print the demo only        norec = play without recording\n"
		"other:  interlope_status, interlope_stop, interlope_debug 1, interlope_cheats 0/1/2,\n"
		"        interlope_record, interlope_noclip, interlope_opts \"k=v ...\", interlope_name,\n"
		"        interlope_host, interlope_port\n");
}

#if SOURCE_ENGINE >= SE_ORANGEBOX
void CInterloperPlugin::CC_Interlope(const CCommand &args)
{
	std::string options = CollectArgs(args);
#else
void CInterloperPlugin::CC_Interlope()
{
	std::string options = CollectArgs();
#endif
	if (options == "help" || options == "?" || options == "-h")
	{
		PrintUsage();
		return;
	}

	// Still a listenserver-only assumption (see header note). Uses the
	// sample's own PEntityOfEntIndex() wrapper rather than a raw INDEXENT
	// macro, since that wrapper is confirmed to handle the L4D-vs-earlier
	// IVEngineServer difference correctly.
	edict_t *pPlayer = PEntityOfEntIndex(1);
	if (!pPlayer || pPlayer->IsFree())
	{
		META_CONPRINT("interlope: no valid player found\n");
		return;
	}

	g_InterloperPlugin.StartInterlope(pPlayer, options);
}

#if SOURCE_ENGINE >= SE_ORANGEBOX
void CInterloperPlugin::CC_InterlopeStop(const CCommand &args)
#else
void CInterloperPlugin::CC_InterlopeStop()
#endif
{
	g_InterloperPlugin.StopInterlope("interlope_stop", false);
}

#if SOURCE_ENGINE >= SE_ORANGEBOX
void CInterloperPlugin::CC_InterlopeStatus(const CCommand &args)
#else
void CInterloperPlugin::CC_InterlopeStatus()
#endif
{
	g_InterloperPlugin.PrintStatus();
}

// -- helpers ---------------------------------------------------------------

// Split "a b c" on spaces.
static std::vector<std::string> SplitWords(const std::string &in)
{
	std::vector<std::string> out;
	std::string cur;
	for (size_t i = 0; i < in.size(); i++)
	{
		const char c = in[i];
		if (c == ' ' || c == '\t' || c == '\r' || c == '\n')
		{
			if (!cur.empty()) { out.push_back(cur); cur.clear(); }
		}
		else
			cur += c;
	}
	if (!cur.empty()) out.push_back(cur);
	return out;
}

// sv_cheats gates noclip, setpos, setang and give. It resets to 0 on every
// map load, which is the most likely reason a teleport-style demo does
// nothing: the commands are silently refused. Turn it on ourselves.
void CInterloperPlugin::EnableCheats()
{
	const int mode = g_pCheatsCvar ? g_pCheatsCvar->GetInt() : 1;
	if (mode == 0)
	{
		DebugLog("interlope_cheats 0: leaving sv_cheats alone");
		return;
	}
	ConVar *sv = icvar ? icvar->FindVar("sv_cheats") : nullptr;
	if (!sv)
	{
		META_CONPRINT("interlope: warning - could not find sv_cheats\n");
		return;
	}
	m_prevCheats = sv->GetInt();
	if (m_prevCheats == 0)
	{
		sv->SetValue(1);
		m_cheatsChangedByUs = (mode == 1);
		META_CONPRINTF("interlope: sv_cheats was OFF, turned it on for the demo%s\n",
			mode == 1 ? " (will be restored)" : "");
	}
	else
		DebugLog("sv_cheats already on");
}

void CInterloperPlugin::RestoreCheats()
{
	if (!m_cheatsChangedByUs)
		return;
	ConVar *sv = icvar ? icvar->FindVar("sv_cheats") : nullptr;
	if (sv)
	{
		sv->SetValue(m_prevCheats);
		DebugLog("sv_cheats restored to %d", m_prevCheats);
	}
	m_cheatsChangedByUs = false;
}

void CInterloperPlugin::PrintRecordingSummary(const interloper::Recording &rec)
{
	META_CONPRINTF("interlope: recording: type %d, commit %s, %zu ticks (~%.1fs at 66/s), %zu events\n",
		rec.demoType,
		rec.commitFormat == interloper::COMMIT_TODAY ? "TODAY" : rec.commitNumber.c_str(),
		rec.ticks.size(), rec.ticks.size() / 66.0, rec.events.size());

	if (rec.ticks.empty())
		return;

	float mn[3], mx[3];
	for (int a = 0; a < 3; a++) { mn[a] = mx[a] = rec.ticks[0].origin[a]; }
	uint32_t buttonsEver = 0;
	for (size_t i = 0; i < rec.ticks.size(); i++)
	{
		const interloper::WireTick &t = rec.ticks[i];
		buttonsEver |= t.buttons;
		for (int a = 0; a < 3; a++)
		{
			if (t.origin[a] < mn[a]) mn[a] = t.origin[a];
			if (t.origin[a] > mx[a]) mx[a] = t.origin[a];
		}
	}
	const interloper::WireTick &f = rec.ticks.front();
	const interloper::WireTick &l = rec.ticks.back();
	META_CONPRINTF("interlope:   first tick pos (%.0f %.0f %.0f) ang (%.0f %.0f %.0f)\n",
		f.origin[0], f.origin[1], f.origin[2], f.angles[0], f.angles[1], f.angles[2]);
	META_CONPRINTF("interlope:   last  tick pos (%.0f %.0f %.0f) ang (%.0f %.0f %.0f)\n",
		l.origin[0], l.origin[1], l.origin[2], l.angles[0], l.angles[1], l.angles[2]);
	META_CONPRINTF("interlope:   path spans %.0f x %.0f x %.0f units, buttons used 0x%x\n",
		mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2], buttonsEver);

	const size_t shown = rec.events.size() < 12 ? rec.events.size() : 12;
	for (size_t i = 0; i < shown; i++)
		META_CONPRINTF("interlope:   event @tick %u: kind %d '%s'\n",
			(unsigned)rec.events[i].tickIndex, (int)rec.events[i].type, rec.events[i].param.c_str());
	if (rec.events.size() > shown)
		META_CONPRINTF("interlope:   ... and %zu more events\n", rec.events.size() - shown);
}

void CInterloperPlugin::PrintStatus()
{
	char gameDir[260] = {0};
	engine->GetGameDir(gameDir, sizeof(gameDir));
	ConVar *sv = icvar ? icvar->FindVar("sv_cheats") : nullptr;

	META_CONPRINTF("interlope status: game dir '%s' (folder '%s'%s)\n", gameDir, GameFolder(), IsTF2() ? ", TF2 mode" : "");
	if (s_playerinfo)
	{
		IPlayerInfo *pi = s_playerinfo->GetPlayerInfo(PEntityOfEntIndex(1));
		if (pi)
			META_CONPRINTF("  player      : team %d, dead %d, observer %d\n", pi->GetTeamIndex(), pi->IsDead() ? 1 : 0, pi->IsObserver() ? 1 : 0);
	}
	META_CONPRINTF("  core server : %s:%d\n",
		g_pHostCvar ? g_pHostCvar->GetString() : "?", g_pPortCvar ? g_pPortCvar->GetInt() : 0);
	META_CONPRINTF("  sv_cheats   : %d   interlope_cheats %d   noclip %d   record %d   dryrun %d   debug %d\n",
		sv ? sv->GetInt() : -1, g_pCheatsCvar->GetInt(), g_pNoclipCvar->GetInt(),
		g_pRecordCvar->GetInt(), g_pDryRunCvar->GetInt(), g_pDebugCvar->GetInt());
	META_CONPRINTF("  type %d   opts '%s'   name '%s'\n",
		g_pTypeCvar->GetInt(), g_pOptsCvar->GetString(), g_pNameCvar->GetString());
	if (m_isPlaying)
		META_CONPRINTF("  PLAYING: type %d commit %s, tick %zu / %zu, %s mode, started=%d\n",
			m_recording.demoType, m_recording.commitNumber.c_str(), m_currentTick,
			m_recording.ticks.size(), m_inputMode ? "input" : "teleport", s_playbackStarted ? 1 : 0);
	else
		META_CONPRINT("  idle\n");
	if (m_posChecks)
		META_CONPRINTF("  last run position check: %zu / %zu ticks matched (worst miss %.1f units)\n",
			m_posMatches, m_posChecks, m_posWorstErr);
}

void CInterloperPlugin::StopInterlope(const char *why, bool finished)
{
	if (!m_isPlaying)
	{
		META_CONPRINT("interlope: nothing is running\n");
		return;
	}
	META_CONPRINTF("interlope: aborting (%s) at tick %zu / %zu\n", why, m_currentTick, m_recording.ticks.size());
	if (m_pTargetPlayer)
	{
		ReleaseAllButtons(m_pTargetPlayer);
		if (m_noclipToggled)
			engine->ClientCommand(m_pTargetPlayer, "noclip\n");
		if (m_recordStarted)
			engine->ClientCommand(m_pTargetPlayer, "stop\n");
	}
	m_noclipToggled = false;
	m_recordStarted = false;
	m_isPlaying = false;
	m_pTargetPlayer = nullptr;
	m_cheatsRestoreCountdown = 30;
	m_live.Close();
	m_liveMode = false;
}

// -- core flow -----------------------------------------------------------

void CInterloperPlugin::StartInterlope(edict_t *pPlayer, const std::string &optionsIn)
{
	if (m_isPlaying)
	{
		META_CONPRINT("interlope: a demo is already being generated/played back (interlope_stop to abort)\n");
		return;
	}

	// Pull the plugin-side flags out of the console arguments; whatever is
	// left is forwarded to the core server verbatim.
	bool dryRun = g_pDryRunCvar->GetInt() != 0;
	m_doRecord = g_pRecordCvar->GetInt() != 0;
	std::string forwarded;
	{
		const std::vector<std::string> words = SplitWords(optionsIn);
		for (size_t i = 0; i < words.size(); i++)
		{
			if (words[i] == "dry")        dryRun = true;
			else if (words[i] == "norec") m_doRecord = false;
			else { if (!forwarded.empty()) forwarded += ' '; forwarded += words[i]; }
		}
	}

	// Defaults from convars come first so anything typed on the command line
	// overrides them (the server keeps the last value of a repeated key).
	std::string options;
	if (g_pTypeCvar->GetInt() > 0)
		options = std::to_string(g_pTypeCvar->GetInt());
	if (g_pOptsCvar->GetString()[0])
		options += (options.empty() ? "" : " ") + std::string(g_pOptsCvar->GetString());
	if (!forwarded.empty())
		options += (options.empty() ? "" : " ") + forwarded;

	interloper::MapBounds bounds;
	if (!GetWorldBounds(bounds))
	{
		META_CONPRINT("interlope: could not determine world bounds\n");
		return;
	}
	DebugLog("world bounds: mins=(%.1f,%.1f,%.1f) maxs=(%.1f,%.1f,%.1f)",
		bounds.mins[0], bounds.mins[1], bounds.mins[2],
		bounds.maxs[0], bounds.maxs[1], bounds.maxs[2]);

	// Where the player is standing right now, captured BEFORE any tick is
	// applied. Sent to the core server so type 1 can twist the camera in
	// place instead of at a random point. GetCollisionOrigin() is the
	// entity's abs origin, the same space setpos writes to.
	ICollideable *pCollide = pPlayer->GetCollideable();
	if (!pCollide)
	{
		META_CONPRINT("interlope: could not read player origin\n");
		return;
	}
	const Vector &playerPos = pCollide->GetCollisionOrigin();
	const float origin[3] = { playerPos.x, playerPos.y, playerPos.z };
	DebugLog("player origin: (%.1f,%.1f,%.1f)", origin[0], origin[1], origin[2]);

	// interlope_port overrides the compiled-in default so the port can be
	// changed from the console without a rebuild.
	if (g_pPortCvar)
	{
		const int port = g_pPortCvar->GetInt();
		if (port >= 1 && port <= 65535)
			m_serverPort = static_cast<uint16_t>(port);
	}
	if (g_pHostCvar && g_pHostCvar->GetString()[0])
		m_serverHost = g_pHostCvar->GetString();

	// This plugin can run Type 4 live sessions.
	if (options.find("live=") == std::string::npos)
		options += (options.empty() ? "" : " ") + std::string("live=1");

	// Tell the server which game this is so type 3 can use the right weapons.
	if (options.find("game=") == std::string::npos)
		options += (options.empty() ? "" : " ") + std::string("game=") + GameFolder();

	if (s_playerinfo)
	{
		IPlayerInfo *pi = s_playerinfo->GetPlayerInfo(pPlayer);
		if (pi)
		{
			DebugLog("player state: team %d dead %d observer %d", pi->GetTeamIndex(), pi->IsDead() ? 1 : 0, pi->IsObserver() ? 1 : 0);
			if (pi->IsDead() || pi->IsObserver() || (IsTF2() && pi->GetTeamIndex() < 2))
				META_CONPRINT("interlope: warning - you are dead, spectating or have no team/class. "
					"Pick a class and spawn first (TF2: choose a team and class), or the demo will not move you.\n");
		}
	}

	const char *mapName = gpGlobals ? STRING(gpGlobals->mapname) : nullptr;
	META_CONPRINTF("interlope: asking %s:%u for '%s' (map %s)\n", m_serverHost.c_str(), m_serverPort,
		options.empty() ? "a random demo" : options.c_str(), mapName ? mapName : "?");

	interloper::NetClient client(m_serverHost, m_serverPort);
	interloper::Recording recording;
	std::string err;

	if (!client.RequestDemo(bounds, mapName ? mapName : "", origin, recording, err, options))
	{
		META_CONPRINTF("interlope: request failed: %s\n", err.c_str());
		META_CONPRINTF("interlope: is server.py running with --port %u ? (interlope_port / interlope_host)\n",
			m_serverPort);
		return;
	}
	DebugLog("sent '%s', server replied '%s'", client.LastRequestLine().c_str(), client.LastResponseLine().c_str());

	if (recording.isLive)
		META_CONPRINTF("interlope: type 4 live session granted (commit %s, port %u)\n",
			recording.commitNumber.c_str(), (unsigned)recording.livePort);
	else
		PrintRecordingSummary(recording);

	if (dryRun)
	{
		META_CONPRINT("interlope: dry run - not playing it\n");
		return;
	}

	EnableCheats();

	m_recording = recording;
	m_currentTick = 0;
	m_pTargetPlayer = pPlayer;
	m_isPlaying = true;

	// Type 3 ("aimless gameplay") is replayed as inputs, not positions; so is
	// Type 4, which is a live conversation with the server.
	m_inputMode = (m_recording.demoType == 3) || m_recording.isLive;
	m_heldButtons = 0;
	m_liveMode = false;
	if (m_recording.isLive)
	{
		if (!m_live.Open(m_serverHost, m_recording.livePort))
		{
			META_CONPRINTF("interlope: could not open the live link to %s:%u\n", m_serverHost.c_str(), (unsigned)m_recording.livePort);
			m_isPlaying = false;
			m_pTargetPlayer = nullptr;
			return;
		}
		m_liveMode = true;
		m_liveButtons = 0;
		m_liveMisses = 0;
		m_liveObsSent = m_liveActsGot = 0;
		m_liveRate = 3.0f;
		m_livePitch = 0.0f;
		m_liveYaw = 0.0f;
		if (s_playerinfo)
		{
			IPlayerInfo *pi = s_playerinfo->GetPlayerInfo(pPlayer);
			if (pi)
			{
				const CBotCmd cmd = pi->GetLastUserCommand();
				m_liveYaw = cmd.viewangles.y;
				m_livePitch = cmd.viewangles.x;
			}
		}
		m_liveTargetYaw = m_liveYaw;
		m_liveTargetPitch = m_livePitch;
	}
	m_recordStarted = false;
	m_noclipToggled = false;
	m_posChecks = m_posMatches = 0;
	m_posWorstErr = 0.0f;
	m_wantedCount = 0;
	m_cheatsRestoreCountdown = 0;

	// Close the console AND the GameUI layer, then unpause. hideconsole alone
	// leaves GameUI up, which keeps the server paused in singleplayer/listen.
	engine->ClientCommand(pPlayer, "hideconsole\n");
	SendUnpause(pPlayer);

	// noclip + record are deferred to Hook_GameFrame until the server is
	// actually simulating (see the gating block there).
	s_playbackStarted = false;
	s_pausedFrames = 0;

	META_CONPRINTF("interlope: starting type %d demo, COMMITTED %s (%zu ticks)%s\n",
		m_recording.demoType,
		m_recording.commitFormat == interloper::COMMIT_TODAY ? "TODAY" : m_recording.commitNumber.c_str(),
		m_recording.ticks.size(), m_doRecord ? "" : " [not recording]");
}

KHook::Return<void> CInterloperPlugin::Hook_GameFrame(IServerGameDLL *, bool simulating)
{
	// Put sv_cheats back a little AFTER the last tick: the final noclip/stop
	// are client commands that run a frame or two later and are cheat-gated.
	if (m_cheatsRestoreCountdown > 0 && --m_cheatsRestoreCountdown == 0)
		RestoreCheats();

	if (!m_isPlaying)
		return { KHook::Action::Ignore };

	// Wait for the server to unpause before doing anything. GameFrame still
	// fires while paused (simulating == false), so without this gate ticks
	// would be applied and recorded against a frozen world.
	if (!s_playbackStarted)
	{
		if (!simulating && s_pausedFrames < UNPAUSE_GIVE_UP_FRAMES)
		{
			if (s_pausedFrames % UNPAUSE_RETRY_INTERVAL == 0)
			{
				DebugLog("still paused (frame %d), re-sending unpause", s_pausedFrames);
				SendUnpause(m_pTargetPlayer);
			}
			s_pausedFrames++;
			return { KHook::Action::Ignore };
		}

		if (!simulating)
			META_CONPRINT("interlope: server never unpaused, starting anyway\n");

		DebugLog("server simulating, beginning playback");
		s_playbackStarted = true;
		m_startTime = gpGlobals ? gpGlobals->curtime : 0.0f;

		// Force noclip before the first tick lands. setpos alone can still
		// plant the player inside solid void geometry on maps where "outside
		// the compiled bounds" isn't open air on every axis (confirmed by a
		// real run: single-axis-only bounds violation put the player in the
		// ground instead of floating). noclip makes the "stuck in the sky"
		// look independent of any specific map's geometry.
		// Input-driven demos need real collision, so no noclip for them.
		if (!m_inputMode && g_pNoclipCvar->GetInt() != 0)
		{
			engine->ClientCommand(m_pTargetPlayer, "noclip\n");
			m_noclipToggled = true;
		}

		if (m_doRecord)
		{
			char cmd[192];
			if (g_pNameCvar->GetString()[0])
				snprintf(cmd, sizeof(cmd), "record %s\n", g_pNameCvar->GetString());
			else
				snprintf(cmd, sizeof(cmd), "record type%d_%s\n",
					m_recording.demoType, m_recording.commitNumber.c_str());
			engine->ClientCommand(m_pTargetPlayer, "%s", cmd);
			m_recordStarted = true;
			DebugLog("issued: %s", cmd);
		}
	}

	if (m_liveMode)
	{
		LiveFrame();
		return { KHook::Action::Ignore };
	}

	// Safety net: shouldn't normally reach here (the last-tick branch below
	// stops playback itself), but covers a recording with zero ticks -
	// ticks.size() - 1 would underflow (size_t) below without this guard.
	if (m_recording.ticks.empty() || m_currentTick >= m_recording.ticks.size())
	{
		DebugLog("recording has no ticks to apply, stopping immediately");
		StopInterlope("no ticks", true);
		return { KHook::Action::Ignore };
	}

	ApplyTick(m_pTargetPlayer, m_recording.ticks[m_currentTick]);

	const bool isLastTick = (m_currentTick == m_recording.ticks.size() - 1);

	if (isLastTick)
	{
		// Finalize the recording BEFORE firing any terminal event on this
		// same tick. This order matters: a real confirmed run showed a
		// KILL event triggers an actual HL2 death, which immediately
		// starts tearing down and reloading the server
		// ("Server shutting down") - faster than the next GameFrame call
		// this used to wait for to issue "stop". That race truncated the
		// .dem file (confirmed: recorded demo cut off early on playback).
		// Stopping first, on the same frame, closes the file cleanly
		// before anything destructive can happen.
		DebugLog("last tick (%zu) reached, finalizing recording before terminal events", m_currentTick);
		if (m_inputMode)
			ReleaseAllButtons(m_pTargetPlayer);
		else if (m_noclipToggled)
			engine->ClientCommand(m_pTargetPlayer, "noclip\n"); // toggle off
		if (m_recordStarted)
			engine->ClientCommand(m_pTargetPlayer, "stop\n");
		m_noclipToggled = false;
		m_recordStarted = false;
	}

	for (const interloper::Event &ev : m_recording.events)
	{
		if (ev.tickIndex == m_currentTick)
			FireEvent(m_pTargetPlayer, ev);
	}

	m_currentTick++;

	if (isLastTick)
	{
		const float elapsed = gpGlobals ? gpGlobals->curtime - m_startTime : 0.0f;
		META_CONPRINTF("interlope: finished type %d, %zu ticks in %.2fs of game time\n",
			m_recording.demoType, m_recording.ticks.size(), elapsed);
		if (!m_inputMode && m_posChecks > 0)
		{
			META_CONPRINTF("interlope: position check: %zu / %zu ticks had the player where we put them (worst miss %.1f units)\n",
				m_posMatches, m_posChecks, m_posWorstErr);
			if (m_posMatches * 10 < m_posChecks)
				META_CONPRINT("interlope: !! setpos is NOT taking effect. Common causes: sv_cheats is 0 (interlope_cheats 0 leaves it alone), "
					"the player is dead/frozen, or the game was paused the whole time. Try: interlope_debug 1, interlope_status\n");
		}
		m_isPlaying = false;
		m_pTargetPlayer = nullptr;
		m_cheatsRestoreCountdown = 30;
	}

	return { KHook::Action::Ignore };
}

void CInterloperPlugin::ReleaseAllButtons(edict_t *pPlayerEdict)
{
	if (!pPlayerEdict)
		return;
	for (const ButtonBinding &b : s_buttonBindings)
	{
		if (m_heldButtons & b.mask)
			engine->ClientCommand(pPlayerEdict, "%s", b.release);
	}
	m_heldButtons = 0;
}

void CInterloperPlugin::ApplyInputTick(edict_t *pPlayerEdict, const interloper::WireTick &tick)
{
	// Only send +/- for buttons that changed since the previous tick.
	const uint32_t changed = tick.buttons ^ m_heldButtons;
	if (changed)
	{
		for (const ButtonBinding &b : s_buttonBindings)
		{
			if (!(changed & b.mask))
				continue;
			engine->ClientCommand(pPlayerEdict, "%s",
				(tick.buttons & b.mask) ? b.press : b.release);
		}
		m_heldButtons = tick.buttons;
	}

	// View angles still go through setang. No setpos: position comes from
	// the engine's own movement code.
	char angCmd[96];
	snprintf(angCmd, sizeof(angCmd), "setang %f %f %f\n",
		tick.angles[0], tick.angles[1], tick.angles[2]);
	engine->ClientCommand(pPlayerEdict, "%s", angCmd);

	DebugLog("tick %zu: buttons=0x%x ang=(%.1f,%.1f,%.1f)",
		m_currentTick, tick.buttons, tick.angles[0], tick.angles[1], tick.angles[2]);
}

void CInterloperPlugin::ApplyTick(edict_t *pPlayerEdict, const interloper::WireTick &tick)
{
	if (m_inputMode)
	{
		ApplyInputTick(pPlayerEdict, tick);
		return;
	}

	// Self-check: where is the player right now versus where the last few
	// ticks asked them to be? Console commands reach the engine a frame or
	// two late, so compare against a short history, not just the last tick.
	if (m_wantedCount > 0)
	{
		ICollideable *pCollide = pPlayerEdict->GetCollideable();
		if (pCollide)
		{
			const Vector &cur = pCollide->GetCollisionOrigin();
			const size_t n = m_wantedCount < (size_t)kWantedHistory ? m_wantedCount : (size_t)kWantedHistory;
			float best = 1e30f;
			for (size_t j = 0; j < n; j++)
			{
				const float dx = cur.x - m_wanted[j][0];
				const float dy = cur.y - m_wanted[j][1];
				const float dz = cur.z - m_wanted[j][2];
				const float d = sqrtf(dx * dx + dy * dy + dz * dz);
				if (d < best) best = d;
			}
			m_posChecks++;
			if (best < 1.0f) m_posMatches++;
			if (best > m_posWorstErr) m_posWorstErr = best;
			DebugLog("tick %zu: player is at (%.1f,%.1f,%.1f), nearest recent target %.1f away",
				m_currentTick, cur.x, cur.y, cur.z, best);
		}
	}
	{
		float *slot = m_wanted[m_wantedCount % kWantedHistory];
		slot[0] = tick.origin[0]; slot[1] = tick.origin[1]; slot[2] = tick.origin[2];
		m_wantedCount++;
	}

	// setpos/setang are old sv_cheats-gated engine console commands. They are
	// sent as ONE command string (';' separated) so a frame can never apply
	// the position without the angle or the other way round.
	char cmd[192];
	snprintf(cmd, sizeof(cmd), "setpos %f %f %f;setang %f %f %f\n",
		tick.origin[0], tick.origin[1], tick.origin[2],
		tick.angles[0], tick.angles[1], tick.angles[2]);
	engine->ClientCommand(pPlayerEdict, "%s", cmd);

	DebugLog("tick %zu: pos=(%.1f,%.1f,%.1f) ang=(%.1f,%.1f,%.1f)",
		m_currentTick, tick.origin[0], tick.origin[1], tick.origin[2],
		tick.angles[0], tick.angles[1], tick.angles[2]);
}


// -- Type 4: live session -------------------------------------------------------

namespace
{
	// Trace filter that ignores the player we are looking out of.
	class CSkipSelfFilter : public ITraceFilter
	{
	public:
		explicit CSkipSelfFilter(IHandleEntity *skip) : m_skip(skip) {}
		bool ShouldHitEntity(IHandleEntity *pEntity, int) { return pEntity != m_skip; }
		TraceType_t GetTraceType() const { return TRACE_EVERYTHING; }
	private:
		IHandleEntity *m_skip;
	};

	void DirFromAngles(float yawDeg, float pitchDeg, float out[3])
	{
		const float y = yawDeg * 3.14159265f / 180.0f;
		const float p = pitchDeg * 3.14159265f / 180.0f;
		out[0] = cosf(p) * cosf(y);
		out[1] = cosf(p) * sinf(y);
		out[2] = -sinf(p); // Source: positive pitch looks down
	}

	float LiveAngleDiff(float a, float b)
	{
		float d = fmodf(a - b + 180.0f, 360.0f);
		if (d < 0.0f) d += 360.0f;
		return d - 180.0f;
	}

	struct NearEnt
	{
		float dist;
		int index;
		float pos[3];
	};
}

// One ray from `from` in a direction; returns distance travelled (maxd if clear).
// If hitIndex is given it receives the entity index of what was hit (-1 = nothing).
static float LiveRay(const float from[3], const float dir[3], float maxd, IHandleEntity *skip, int *hitIndex)
{
	if (hitIndex) *hitIndex = -1;
	if (!s_trace)
		return maxd;
	Vector start(from[0], from[1], from[2]);
	Vector end(from[0] + dir[0] * maxd, from[1] + dir[1] * maxd, from[2] + dir[2] * maxd);
	Ray_t ray;
	ray.Init(start, end);
	CSkipSelfFilter filter(skip);
	trace_t tr;
	s_trace->TraceRay(ray, MASK_SOLID, &filter, &tr);
	if (hitIndex && tr.m_pEnt && s_gameents)
	{
		edict_t *e = s_gameents->BaseEntityToEdict(tr.m_pEnt);
		if (e)
			*hitIndex = IndexOfEdict(e);
	}
	return tr.fraction * maxd;
}

static bool WantedClass(const char *cn, bool isSelfSlot)
{
	if (!cn || !cn[0] || isSelfSlot)
		return false;
	static const char *const prefixes[] =
	{
		"npc_", "item_", "weapon_", "func_button", "prop_button", "prop_floor_button",
		"prop_under_button", "prop_weighted_cube", "prop_monster_box", "func_door",
		"prop_door_rotating", "obj_", "tf_ammo", "tf_bot", "player",
	};
	for (size_t i = 0; i < sizeof(prefixes) / sizeof(prefixes[0]); i++)
		if (strncmp(cn, prefixes[i], strlen(prefixes[i])) == 0)
			return true;
	return false;
}

void CInterloperPlugin::BuildObservation(std::string &out)
{
	edict_t *me = m_pTargetPlayer;
	IPlayerInfo *pi = s_playerinfo ? s_playerinfo->GetPlayerInfo(me) : nullptr;

	float org[3] = { 0, 0, 0 };
	int hp = 100;
	const char *weapon = "none";
	if (pi)
	{
		const Vector o = pi->GetAbsOrigin();
		org[0] = o.x; org[1] = o.y; org[2] = o.z;
		hp = pi->GetHealth();
		if (pi->IsDead())
			hp = 0;
		const char *w = pi->GetWeaponName();
		if (w && w[0])
			weapon = w;
	}
	else if (me && me->GetCollideable())
	{
		const Vector &o = me->GetCollideable()->GetCollisionOrigin();
		org[0] = o.x; org[1] = o.y; org[2] = o.z;
	}

	IHandleEntity *self = me ? (IHandleEntity *)me->GetUnknown() : nullptr;
	const float eye[3] = { org[0], org[1], org[2] + 64.0f };

	// Rays at the commanded view angles: ahead, 45 and 90 degrees to each side.
	float dir[3];
	DirFromAngles(m_liveYaw, 0.0f, dir);
	const float rf = LiveRay(eye, dir, 1024.0f, self, nullptr);
	DirFromAngles(m_liveYaw + 45.0f, 0.0f, dir);
	const float rl = LiveRay(eye, dir, 1024.0f, self, nullptr);
	DirFromAngles(m_liveYaw - 45.0f, 0.0f, dir);
	const float rr = LiveRay(eye, dir, 1024.0f, self, nullptr);
	DirFromAngles(m_liveYaw + 90.0f, 0.0f, dir);
	const float rl2 = LiveRay(eye, dir, 1024.0f, self, nullptr);
	DirFromAngles(m_liveYaw - 90.0f, 0.0f, dir);
	const float rr2 = LiveRay(eye, dir, 1024.0f, self, nullptr);

	// Ledge probes: floor distance below points 64 and 140 units ahead. A flat
	// floor reads about 0; a drop reads big.
	float fd = 0.0f;
	{
		float flat[3];
		DirFromAngles(m_liveYaw, 0.0f, flat);
		const float reach[2] = { 64.0f, 140.0f };
		const float down[3] = { 0.0f, 0.0f, -1.0f };
		for (int i = 0; i < 2; i++)
		{
			const float probe[3] = { org[0] + flat[0] * reach[i], org[1] + flat[1] * reach[i], org[2] + 16.0f };
			const float d = LiveRay(probe, down, 600.0f, self, nullptr) - 16.0f;
			if (d > fd) fd = d;
		}
	}

	// What the crosshair is on.
	int hitIdx = -1;
	DirFromAngles(m_liveYaw, m_livePitch, dir);
	const float hd = LiveRay(eye, dir, 2048.0f, self, &hitIdx);
	const char *hc = "none";
	if (hitIdx >= 0)
	{
		edict_t *he = PEntityOfEntIndex(hitIdx);
		if (he && !he->IsFree() && he->GetClassName() && he->GetClassName()[0])
			hc = he->GetClassName();
	}
	else if (hd < 2047.0f)
		hc = "worldspawn";

	// Nearby entities worth reacting to (up to 16, nearest first).
	const int kMaxEnts = 16;
	NearEnt nearEnts[kMaxEnts];
	int nNear = 0;
	const int maxEnt = gpGlobals ? gpGlobals->maxEntities : 0;
	for (int i = 2; i < maxEnt; i++)
	{
		edict_t *e = PEntityOfEntIndex(i);
		if (!e || e->IsFree())
			continue;
		const char *cn = e->GetClassName();
		if (!WantedClass(cn, false))
			continue;
		ICollideable *c = e->GetCollideable();
		if (!c || c->GetSolid() == SOLID_NONE)
			continue; // carried weapons and hidden things have no solid
		const Vector &p = c->GetCollisionOrigin();
		const float dx = p.x - org[0], dy = p.y - org[1], dz = p.z - org[2];
		const float dist = sqrtf(dx * dx + dy * dy + dz * dz);
		if (dist > 1600.0f)
			continue;
		// keep the nearest kMaxEnts by insertion
		int pos = nNear;
		while (pos > 0 && nearEnts[pos - 1].dist > dist) pos--;
		if (pos >= kMaxEnts)
			continue;
		const int last = (nNear < kMaxEnts) ? nNear : kMaxEnts - 1;
		for (int k = last; k > pos; k--) nearEnts[k] = nearEnts[k - 1];
		nearEnts[pos].dist = dist; nearEnts[pos].index = i;
		nearEnts[pos].pos[0] = p.x; nearEnts[pos].pos[1] = p.y; nearEnts[pos].pos[2] = p.z;
		if (nNear < kMaxEnts) nNear++;
	}

	std::string ents;
	for (int k = 0; k < nNear; k++)
	{
		edict_t *e = PEntityOfEntIndex(nearEnts[k].index);
		// line of sight from the eye to the entity's origin (+ a little up)
		float to[3] = { nearEnts[k].pos[0] - eye[0], nearEnts[k].pos[1] - eye[1], nearEnts[k].pos[2] + 24.0f - eye[2] };
		const float len = sqrtf(to[0] * to[0] + to[1] * to[1] + to[2] * to[2]);
		int vis = 0;
		if (len > 1.0f)
		{
			to[0] /= len; to[1] /= len; to[2] /= len;
			int hit = -1;
			const float d = LiveRay(eye, to, len, self, &hit);
			vis = (d >= len - 8.0f || hit == nearEnts[k].index) ? 1 : 0;
		}
		char buf[160];
		snprintf(buf, sizeof(buf), "%s%s,%.0f,%.0f,%.0f,%d", ents.empty() ? "" : "|",
			e->GetClassName(), nearEnts[k].pos[0], nearEnts[k].pos[1], nearEnts[k].pos[2], vis);
		ents += buf;
	}

	const float now = gpGlobals ? gpGlobals->curtime : 0.0f;
	char head[640];
	snprintf(head, sizeof(head),
		"OBS gt=%.3f x=%.1f y=%.1f z=%.1f yaw=%.1f pitch=%.1f hp=%d w=%s "
		"rf=%.0f rl=%.0f rr=%.0f rl2=%.0f rr2=%.0f fd=%.0f hc=%s hd=%.0f ents=",
		now, org[0], org[1], org[2], m_liveYaw, m_livePitch, hp, weapon,
		rf, rl, rr, rl2, rr2, fd, hc, hd);
	out = head;
	out += ents;
	out += "\n";
}

// "ACT b=<mask> yaw=<deg> pitch=<deg> rate=<deg/tick> [cmd=slotN]"
void CInterloperPlugin::ApplyLiveAction(const std::string &line)
{
	float yaw = m_liveTargetYaw, pitch = m_liveTargetPitch, rate = m_liveRate;
	uint32_t buttons = m_liveButtons;
	std::string cmd;

	size_t pos = 0;
	while (pos < line.size())
	{
		size_t sp = line.find(' ', pos);
		if (sp == std::string::npos) sp = line.size();
		const std::string tok = line.substr(pos, sp - pos);
		pos = sp + 1;
		const size_t eq = tok.find('=');
		if (eq == std::string::npos)
			continue;
		const std::string key = tok.substr(0, eq);
		const char *val = tok.c_str() + eq + 1;
		if (key == "b")          buttons = (uint32_t)strtoul(val, nullptr, 10);
		else if (key == "yaw")   yaw = (float)atof(val);
		else if (key == "pitch") pitch = (float)atof(val);
		else if (key == "rate")  rate = (float)atof(val);
		else if (key == "cmd")   cmd = val;
	}

	if (pitch > 89.0f) pitch = 89.0f;
	if (pitch < -89.0f) pitch = -89.0f;
	if (rate < 0.2f) rate = 0.2f;
	if (rate > 20.0f) rate = 20.0f;
	m_liveTargetYaw = yaw;
	m_liveTargetPitch = pitch;
	m_liveRate = rate;
	m_liveButtons = buttons;

	// Only a short whitelist of commands is ever run on the server's say-so.
	if (!cmd.empty() && m_pTargetPlayer)
	{
		const bool slot = cmd.size() == 5 && cmd.compare(0, 4, "slot") == 0 && cmd[4] >= '1' && cmd[4] <= '9';
		if (slot || cmd == "lastinv" || cmd == "invnext" || cmd == "invprev")
		{
			char buf[32];
			snprintf(buf, sizeof(buf), "%s\n", cmd.c_str());
			engine->ClientCommand(m_pTargetPlayer, "%s", buf);
		}
	}
}

void CInterloperPlugin::FinishLive(const char *why)
{
	META_CONPRINTF("interlope: live session over (%s): %zu ticks, %zu observations sent, %zu actions received\n",
		why, m_currentTick, m_liveObsSent, m_liveActsGot);
	if (m_pTargetPlayer)
	{
		ReleaseAllButtons(m_pTargetPlayer);
		if (m_recordStarted)
			engine->ClientCommand(m_pTargetPlayer, "stop\n");
	}
	m_recordStarted = false;
	m_live.Close();
	m_liveMode = false;
	m_isPlaying = false;
	m_pTargetPlayer = nullptr;
	m_cheatsRestoreCountdown = 30;
}

void CInterloperPlugin::LiveFrame()
{
	edict_t *p = m_pTargetPlayer;
	if (!p)
	{
		FinishLive("player gone");
		return;
	}

	// An observation every few ticks (about 11 per second at 66 ticks).
	static const size_t kObsEvery = 6;
	if (m_currentTick % kObsEvery == 0)
	{
		std::string obs;
		BuildObservation(obs);
		std::string reply;
		bool got = false;
		if (m_live.SendLine(obs))
		{
			m_liveObsSent++;
			// The server answers in well under a millisecond on localhost. If it is
			// late, keep the last action; if several arrived, only the newest counts.
			got = m_live.RecvLine(reply, 40);
			std::string newer;
			while (got && m_live.RecvLine(newer, 0))
				reply = newer;
		}
		if (!m_live.IsOpen())
		{
			FinishLive("live link closed");
			return;
		}
		if (got)
		{
			m_liveMisses = 0;
			if (reply.compare(0, 3, "END") == 0)
			{
				FinishLive("server ended the session");
				return;
			}
			if (reply.compare(0, 3, "ACT") == 0)
			{
				m_liveActsGot++;
				ApplyLiveAction(reply);
				DebugLog("live: %s", reply.c_str());
			}
		}
		else if (++m_liveMisses > 60)
		{
			FinishLive("no answer from the server");
			return;
		}
	}

	// Ease the camera toward the requested angles at most m_liveRate degrees per tick.
	const float dy = LiveAngleDiff(m_liveTargetYaw, m_liveYaw) * 0.25f;
	const float dp = (m_liveTargetPitch - m_livePitch) * 0.25f;
	m_liveYaw += (dy > m_liveRate) ? m_liveRate : ((dy < -m_liveRate) ? -m_liveRate : dy);
	m_livePitch += (dp > m_liveRate) ? m_liveRate : ((dp < -m_liveRate) ? -m_liveRate : dp);
	m_liveYaw = fmodf(m_liveYaw, 360.0f);

	interloper::WireTick t;
	memset(&t, 0, sizeof(t));
	t.buttons = m_liveButtons;
	t.angles[0] = m_livePitch;
	t.angles[1] = m_liveYaw;
	ApplyInputTick(p, t);
	m_currentTick++;
}

void CInterloperPlugin::FireEvent(edict_t *pPlayerEdict, const interloper::Event &ev)
{
	switch (ev.type)
	{
		case interloper::EVENT_KILL:
			DebugLog("tick %zu: firing KILL event", m_currentTick);
			engine->ClientCommand(pPlayerEdict, "kill\n");
			break;

		case interloper::EVENT_SOUND:
			DebugLog("tick %zu: firing SOUND event, param='%s'", m_currentTick, ev.param.c_str());
			{
				// Symbolic sounds: "delete" is the type 6 deletion sound, and
				// "chime:N" picks from the type 7 chime list. Ship the chime as
				// sound/interloper/chime.wav in the mod folder.
				static const char *const s_chimeSounds[] =
				{
					"interloper/chime.wav",
				};
				const size_t chimeCount = sizeof(s_chimeSounds) / sizeof(s_chimeSounds[0]);

				std::string sound = ev.param;
				if (sound == "delete")
				{
					sound = "friends/friend_online.wav";
				}
				else if (sound.compare(0, 6, "chime:") == 0)
				{
					const int n = atoi(sound.c_str() + 6);
					sound = s_chimeSounds[static_cast<size_t>(n < 0 ? 0 : n) % chimeCount];
				}

				char cmd[160];
				snprintf(cmd, sizeof(cmd), "play %s\n", sound.c_str());
				engine->ClientCommand(pPlayerEdict, "%s", cmd);
			}
			break;

		case interloper::EVENT_WEAPON_EQUIP:
			DebugLog("tick %zu: firing WEAPON_EQUIP event, param='%s'", m_currentTick, ev.param.c_str());
			{
				// "slotN" params are weapon-slot selection, sent as-is;
				// anything else is an entity name to give.
				// Half-Life: Source (game folder containing "hl1") uses the
				// original HL1 weapon names instead of the HL2 ones.
				std::string weapon = ev.param;
				if (IsTF2() && weapon.compare(0, 4, "slot") != 0)
				{
					// TF2 has no `give` for weapons and no cubemap weapon: the
					// class loadout is what you get; only slot switches apply.
					DebugLog("tf2: skipping give '%s'", weapon.c_str());
					break;
				}
				{
					char gameDir[260] = {0};
					engine->GetGameDir(gameDir, sizeof(gameDir));
					if (strstr(gameDir, "hl1") != nullptr)
					{
						if (weapon == "weapon_pistol")        weapon = "weapon_glock";
						else if (weapon == "weapon_smg1")     weapon = "weapon_mp5";
						else if (weapon == "weapon_shotgun")  weapon = "weapon_shotgun";
					}
				}

				char cmd[160];
				if (weapon.compare(0, 4, "slot") == 0)
					snprintf(cmd, sizeof(cmd), "%s\n", weapon.c_str());
				else
					snprintf(cmd, sizeof(cmd), "give %s\n", weapon.c_str());
				engine->ClientCommand(pPlayerEdict, "%s", cmd);

				// weapon_cubemap only shows its reflective balls once it is the
				// active weapon, so draw it right after giving it.
				if (ev.param == "weapon_cubemap")
					engine->ClientCommand(pPlayerEdict, "use weapon_cubemap\n");
			}
			break;

		case interloper::EVENT_ENTITY_DELETE:
		case interloper::EVENT_ENTITY_SPAWN:
			DebugLog("tick %zu: entity spawn/delete event not yet implemented", m_currentTick);
			break;
	}
}

bool CInterloperPlugin::GetWorldBounds(interloper::MapBounds &out)
{
	edict_t *pWorld = PEntityOfEntIndex(0);
	if (!pWorld)
		return false;

	const model_t *pModel = pWorld->GetCollideable()->GetCollisionModel();
	if (!pModel)
		return false;

	Vector mins, maxs;
	modelinfo->GetModelBounds(pModel, mins, maxs);

	out.mins[0] = mins.x; out.mins[1] = mins.y; out.mins[2] = mins.z;
	out.maxs[0] = maxs.x; out.maxs[1] = maxs.y; out.maxs[2] = maxs.z;
	return true;
}