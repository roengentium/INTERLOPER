// recording.h
//
// C++ mirror of the core server's recording.py wire format. Every field
// layout and byte order here must stay in lockstep with the Python side -
// if you change one, change the other.
//
// Deliberately plain packed structs + manual parsing, no external
// serialization library, to keep this buildable against an old-era SDK
// toolchain with nothing but the standard library.

#ifndef INTERLOPER_RECORDING_H
#define INTERLOPER_RECORDING_H

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace interloper
{

#pragma pack(push, 1)

struct WireHeader
{
	char magic[4];        // "ILDR"
	uint8_t demoType;      // 1-7
	uint8_t commitFormat;  // 0 = standard "##-##-##", 1 = TODAY (Type 7 only)
	char commitNumber[8];  // ascii, null-padded
	uint32_t tickCount;
};

struct WireTick
{
	float origin[3];
	float angles[3]; // pitch, yaw, roll
	uint32_t buttons;
	float move[3];   // forward, side, up
};

struct WireEventHeader
{
	uint32_t tickIndex;
	uint8_t eventType;
	uint8_t paramLen;
	// paramLen bytes follow immediately after this struct in the stream
};

#pragma pack(pop)

static const char kMagic[4] = { 'I', 'L', 'D', 'R' };

enum EventType
{
	EVENT_KILL = 0,
	EVENT_SOUND = 1,
	EVENT_WEAPON_EQUIP = 2,
	EVENT_ENTITY_DELETE = 3,
	EVENT_ENTITY_SPAWN = 4,
};

enum CommitFormat
{
	COMMIT_STANDARD = 0,
	COMMIT_TODAY = 1,
};

struct Event
{
	uint32_t tickIndex;
	EventType type;
	std::string param;
};

struct Recording
{
	uint8_t demoType;
	CommitFormat commitFormat;
	std::string commitNumber; // trimmed of null padding
	std::vector<WireTick> ticks;
	std::vector<Event> events;

	// Type 4 is not a recording: the server hands back a live port instead and
	// the plugin plays the demo as a conversation (observations out, actions in).
	bool isLive = false;
	uint16_t livePort = 0;
};

// Parses a raw byte buffer (as received from the pull socket) into a
// Recording. Returns false on malformed/truncated data instead of throwing,
// since this runs inside the engine's frame loop indirectly and a thrown
// exception here would be a bad time.
inline bool ParseRecording(const uint8_t *data, size_t len, Recording &out)
{
	if (len < sizeof(WireHeader))
		return false;

	WireHeader header;
	std::memcpy(&header, data, sizeof(WireHeader));

	if (std::memcmp(header.magic, kMagic, 4) != 0)
		return false;

	size_t offset = sizeof(WireHeader);

	out.demoType = header.demoType;
	out.commitFormat = static_cast<CommitFormat>(header.commitFormat);

	// commitNumber is null-padded ascii, trim at first \0
	out.commitNumber.assign(header.commitNumber,
		strnlen(header.commitNumber, sizeof(header.commitNumber)));

	out.ticks.clear();
	out.ticks.reserve(header.tickCount);
	for (uint32_t i = 0; i < header.tickCount; ++i)
	{
		if (offset + sizeof(WireTick) > len)
			return false;

		WireTick tick;
		std::memcpy(&tick, data + offset, sizeof(WireTick));
		out.ticks.push_back(tick);
		offset += sizeof(WireTick);
	}

	if (offset + sizeof(uint32_t) > len)
		return false;

	uint32_t eventCount;
	std::memcpy(&eventCount, data + offset, sizeof(uint32_t));
	offset += sizeof(uint32_t);

	out.events.clear();
	out.events.reserve(eventCount);
	for (uint32_t i = 0; i < eventCount; ++i)
	{
		if (offset + sizeof(WireEventHeader) > len)
			return false;

		WireEventHeader eh;
		std::memcpy(&eh, data + offset, sizeof(WireEventHeader));
		offset += sizeof(WireEventHeader);

		if (offset + eh.paramLen > len)
			return false;

		Event ev;
		ev.tickIndex = eh.tickIndex;
		ev.type = static_cast<EventType>(eh.eventType);
		ev.param.assign(reinterpret_cast<const char *>(data + offset), eh.paramLen);
		offset += eh.paramLen;

		out.events.push_back(ev);
	}

	return true;
}

} // namespace interloper

#endif // INTERLOPER_RECORDING_H
