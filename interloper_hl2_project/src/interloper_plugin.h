// interloper_plugin.h
//
// Metamod:Source plugin, built against the current metamod-source repo's
// unified `core` (the historical 1.4/1.6 API split is handled internally
// via engine_wrappers.h's macro shims, confirmed by inspecting the repo's
// own s1_sample_mm), targeting the `episode1` SDK/manifest for a genuine
// circa-2005 Source engine build.
//
// This version is built directly against s1_sample_mm's real, compiling
// structure rather than reconstructed from documentation - specifically:
//   - KHook (third_party/khook) for hooking, not SourceHook's SH_ADD_HOOK.
//   - engine_wrappers.h reused verbatim for the GetEngineFactory/
//     engineFactory macro shim and PEntityOfEntIndex().
//   - gpGlobals is explicitly assigned in Load() (a real bug in an earlier
//     draft of this plugin - it was referenced but never set).
//   - ConCommand/ConVar registration via a BaseAccessor + OneTimeInit/
//     ConVar_Register, which the sample does and an earlier draft of this
//     plugin omitted entirely (meaning "interlope" would likely never
//     have actually registered with the engine before).
//
// STILL UNVERIFIED (flagging honestly rather than guessing):
//   - The exact ConCommand callback signature/registration shape for a
//     *custom* command (not just a ConVar) on the episode1/SE_DARKMESSIAH-
//     and-below API level. The sample only shows a ConVar being
//     registered this way; a ConCommand should follow the same
//     BaseAccessor path since both derive from ConCommandBase, but this
//     hasn't been confirmed by an actual compiling example.
//   - "which player issued this console command" resolution - still using
//     a listenserver-only assumption (PEntityOfEntIndex(1)).

#ifndef INTERLOPER_PLUGIN_H
#define INTERLOPER_PLUGIN_H

#include <ISmmPlugin.h>
#include <eiface.h>
#include <convar.h>
#include "engine_wrappers.h"

#include <string>

#include "recording.h"
#include "net_client.h"

#if defined WIN32 && !defined snprintf
#define snprintf _snprintf
#endif

class CInterloperPlugin : public ISmmPlugin, public IMetamodListener
{
public:
	CInterloperPlugin();

	bool Load(PluginId id, ISmmAPI *ismm, char *error, size_t maxlen, bool late);
	bool Unload(char *error, size_t maxlen);
	bool Pause(char *error, size_t maxlen);
	bool Unpause(char *error, size_t maxlen);

public:
	KHook::Return<void> Hook_GameFrame(IServerGameDLL *, bool simulating);

public:
	const char *GetAuthor() { return "you"; }
	const char *GetName() { return "Interloper Demo Generator"; }
	const char *GetDescription() { return "Generates and records INTERLOPE demos"; }
	const char *GetURL() { return ""; }
	const char *GetLicense() { return "Unlicensed / personal project"; }
	const char *GetVersion() { return "0.1.0"; }
	const char *GetDate() { return __DATE__; }
	const char *GetLogTag() { return "INTERLOPE"; }

	// Console command entry point for "interlope". Confirmed via a real
	// compiler error (FnCommandCallback rejected a `const CCommand &`
	// parameter) that this API level's ConCommand callbacks take no
	// arguments at all - matches the sample's own SOURCE_ENGINE-gated
	// Hook_ClientCommand pattern.
#if SOURCE_ENGINE >= SE_ORANGEBOX
	static void CC_Interlope(const CCommand &args);
#else
	static void CC_Interlope();
#endif

	// interlope_stop / interlope_status: abort a running demo, dump state.
#if SOURCE_ENGINE >= SE_ORANGEBOX
	static void CC_InterlopeStop(const CCommand &args);
	static void CC_InterlopeStatus(const CCommand &args);
#else
	static void CC_InterlopeStop();
	static void CC_InterlopeStatus();
#endif

	void DebugLog(const char *fmt, ...);

private:
	void StartInterlope(edict_t *pPlayer, const std::string &options);
	void StopInterlope(const char *why, bool finished);
	void PrintStatus();
	void PrintRecordingSummary(const interloper::Recording &rec);
	void EnableCheats();
	void RestoreCheats();

	// Type 4 (live): observations out, actions in.
	void LiveFrame();
	void FinishLive(const char *why);
	void BuildObservation(std::string &out);
	void ApplyLiveAction(const std::string &line);
	void ApplyTick(edict_t *pPlayer, const interloper::WireTick &tick);
	void FireEvent(edict_t *pPlayer, const interloper::Event &ev);
	void ApplyInputTick(edict_t *pPlayer, const interloper::WireTick &tick);
	void ReleaseAllButtons(edict_t *pPlayer);
	bool GetWorldBounds(interloper::MapBounds &out);

	bool m_isPlaying = false;
	size_t m_currentTick = 0;
	interloper::Recording m_recording;
	edict_t *m_pTargetPlayer = nullptr;

	// Input-driven playback (type 3): instead of teleporting the player every
	// tick, the recording's button bitmask is turned into +/-commands so the
	// real engine handles movement, collision and weapon fire.
	bool m_inputMode = false;
	uint32_t m_heldButtons = 0;

	// Per-run bookkeeping for the self-check and cleanup.
	bool   m_recordStarted = false;     // we issued "record"
	bool   m_noclipToggled = false;     // we toggled noclip on and owe a toggle off
	bool   m_cheatsChangedByUs = false; // we flipped sv_cheats on
	int    m_prevCheats = 0;
	bool   m_doRecord = true;
	size_t m_posChecks = 0;             // ticks we could compare
	size_t m_posMatches = 0;            // ticks where the player was where we last put them
	float  m_posWorstErr = 0.0f;
	float  m_startTime = 0.0f;
	int    m_cheatsRestoreCountdown = 0; // frames until sv_cheats is put back
	static const int kWantedHistory = 16;
	float  m_wanted[kWantedHistory][3];
	size_t m_wantedCount = 0;

	// Type 4 live session state.
	interloper::LiveLink m_live;
	bool     m_liveMode = false;
	float    m_liveYaw = 0.0f, m_livePitch = 0.0f;       // what the camera is at now
	float    m_liveTargetYaw = 0.0f, m_liveTargetPitch = 0.0f;
	float    m_liveRate = 3.0f;                          // max degrees per tick toward the target
	uint32_t m_liveButtons = 0;
	int      m_liveMisses = 0;                           // consecutive observations with no reply
	size_t   m_liveObsSent = 0;
	size_t   m_liveActsGot = 0;
	float    m_lastObsTime = 0.0f;

	std::string m_serverHost = "127.0.0.1";
	uint16_t m_serverPort = 27500;

	KHook::Virtual<IServerGameDLL, void, bool> m_GameFrame;
};

extern CInterloperPlugin g_InterloperPlugin;
PLUGIN_GLOBALVARS();

#endif // INTERLOPER_PLUGIN_H
